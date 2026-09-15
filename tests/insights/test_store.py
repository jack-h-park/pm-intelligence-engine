import hashlib

import pytest
from pydantic import ValidationError

from app.models.insights import (
    EvidenceBackfillRequest,
    EvidenceBackfillTarget,
    InsightRevision,
    PreparedContext,
    SourceRecord,
)
from app.storage.insight_store import IdempotencyConflict, MissingInsightRecord


def _save_fixture_insight(store, candidate_payload, source_payload, bundle_payload):
    candidate = store.save_candidate(candidate_payload)
    source = store.save_source({**source_payload, "candidate_id": candidate.candidate_id})
    bundle = store.save_bundle(bundle_payload(candidate.candidate_id, source.source_id))
    prepared = store.save_prepared_context(
        PreparedContext(
            candidate_id=candidate.candidate_id,
            bundle_id=bundle.bundle_id,
            question="What is the learning?",
            validation_status="valid",
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )
    insight = store.save_insight(
        InsightRevision(
            prepared_context_id=prepared.prepared_context_id,
            headline="A bounded learning",
            explanation="The source supports a limited observation.",
            actual_change="A new practice was reported.",
            why_now="The candidate was submitted now.",
            personal_relevance="It addresses the question.",
            takeaway="Try a small test.",
            claims=[{"text": "A practice was reported.", "passage_ids": ["passage-fixture-1"]}],
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )
    return candidate, insight


def _backfill_targets():
    return [
        EvidenceBackfillTarget(
            url="https://www.group-ib.com/blog/vwork-app-cloning-gigabud-goldfactory/",
            purpose="primary_incident",
            question="What campaign facts and malware behaviour are directly observed?",
        ),
        EvidenceBackfillTarget(
            url="https://source.android.com/docs/devices/admin/managed-profiles",
            purpose="platform_behavior",
            question="What work-profile behaviour is documented by Android?",
        ),
    ]


def test_backfill_request_persists_idempotently_and_derives_base_candidate(
    store_factory, candidate_payload, source_payload, bundle_payload
):
    """Catches a backfill write that trusts a caller's candidate or mutates its base Insight."""
    store = store_factory()
    candidate, base = _save_fixture_insight(
        store, candidate_payload, source_payload, bundle_payload
    )
    base_before = store.get_insight(base.insight_id).model_dump(mode="json")

    first, first_status = store.create_idempotent_backfill(
        "fixture-reviewer",
        "gigabud-backfill-1",
        "request-hash-1",
        insight_id=base.insight_id,
        base_revision=base.revision,
        targets=_backfill_targets(),
    )
    repeated, repeated_status = store.create_idempotent_backfill(
        "fixture-reviewer",
        "gigabud-backfill-1",
        "request-hash-1",
        insight_id=base.insight_id,
        base_revision=base.revision,
        targets=_backfill_targets(),
    )

    assert first_status == 201
    assert repeated_status == 200
    assert repeated == first
    assert first.candidate_id == candidate.candidate_id
    assert store.get_backfill(first.backfill_id) == first
    assert store.get_insight(base.insight_id).model_dump(mode="json") == base_before


def test_backfill_target_validation_rejects_unsafe_or_duplicate_targets():
    """Catches a request that can make the adapter fetch an unbounded or duplicate URL."""
    with pytest.raises(ValidationError, match="absolute HTTP\\(S\\) URL"):
        EvidenceBackfillTarget(
            url="file:///private/source",
            purpose="primary_incident",
            question="What is directly observed?",
        )

    with pytest.raises(ValidationError, match="unique"):
        EvidenceBackfillRequest(
            insight_id="insight-1",
            base_revision=1,
            candidate_id="candidate-1",
            actor_fingerprint="fixture-reviewer",
            idempotency_key="duplicate-targets",
            request_hash="request-hash-duplicate-targets",
            targets=[
                EvidenceBackfillTarget(
                    url="https://EXAMPLE.test/report/",
                    purpose="primary_incident",
                    question="What is directly observed?",
                ),
                EvidenceBackfillTarget(
                    url="https://example.test/report",
                    purpose="platform_behavior",
                    question="What is documented?",
                ),
            ],
        )

    with pytest.raises(ValidationError, match="at most 3 items"):
        EvidenceBackfillRequest(
            insight_id="insight-1",
            base_revision=1,
            candidate_id="candidate-1",
            actor_fingerprint="fixture-reviewer",
            idempotency_key="too-many-targets",
            request_hash="request-hash-too-many-targets",
            targets=[
                EvidenceBackfillTarget(
                    url=f"https://example.test/report-{index}",
                    purpose="primary_incident",
                    question="What is directly observed?",
                )
                for index in range(4)
            ],
        )


def test_backfill_rejects_missing_insight_and_mismatched_revision(
    store_factory, candidate_payload, source_payload, bundle_payload
):
    """Catches a request being attached to an arbitrary or stale base revision."""
    store = store_factory()

    with pytest.raises(MissingInsightRecord, match="insight missing-insight was not found"):
        store.create_idempotent_backfill(
            "fixture-reviewer", "missing-insight", "request-hash-missing",
            insight_id="missing-insight", base_revision=1, targets=_backfill_targets(),
        )

    _, base = _save_fixture_insight(store, candidate_payload, source_payload, bundle_payload)
    with pytest.raises(MissingInsightRecord, match="revision 2"):
        store.create_idempotent_backfill(
            "fixture-reviewer", "wrong-revision", "request-hash-wrong-revision",
            insight_id=base.insight_id, base_revision=2, targets=_backfill_targets(),
        )


def test_backfill_rejects_idempotency_key_reused_with_different_content(
    store_factory, candidate_payload, source_payload, bundle_payload
):
    """Catches a retry key being reused to widen the approved fetch allowlist."""
    store = store_factory()
    _, base = _save_fixture_insight(store, candidate_payload, source_payload, bundle_payload)
    store.create_idempotent_backfill(
        "fixture-reviewer", "reused-key", "request-hash-one",
        insight_id=base.insight_id, base_revision=base.revision, targets=_backfill_targets(),
    )

    with pytest.raises(IdempotencyConflict, match="different content"):
        store.create_idempotent_backfill(
            "fixture-reviewer", "reused-key", "request-hash-two",
            insight_id=base.insight_id,
            base_revision=base.revision,
            targets=[
                EvidenceBackfillTarget(
                    url="https://example.test/unapproved-expansion",
                    purpose="independent_corroboration",
                    question="What independently corroborates the incident?",
                )
            ],
        )


def test_bundle_survives_restart(store_factory, candidate_payload, source_payload, bundle_payload):
    store = store_factory()
    candidate = store.save_candidate(candidate_payload)
    source = store.save_source({**source_payload, "candidate_id": candidate.candidate_id})
    bundle = store.save_bundle(bundle_payload(candidate.candidate_id, source.source_id))

    restarted = store_factory()

    assert restarted.get_bundle(bundle.bundle_id).model_dump() == bundle.model_dump()
    assert restarted.get_candidate(candidate.candidate_id).model_dump() == candidate.model_dump()


def test_schema_initialization_is_idempotent_and_preserves_legacy_tables(store_factory):
    store = store_factory()
    store.initialize_schema()
    store.initialize_schema()

    with store.engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE legacy_fixture (value TEXT NOT NULL)")
        connection.exec_driver_sql("INSERT INTO legacy_fixture (value) VALUES ('preserved')")

    restarted = store_factory()
    restarted.initialize_schema()

    with restarted.engine.connect() as connection:
        value = connection.exec_driver_sql("SELECT value FROM legacy_fixture").scalar_one()
    assert value == "preserved"


def test_same_url_with_changed_hash_creates_a_new_immutable_source_version(
    store_factory, candidate_payload, source_payload
):
    store = store_factory()
    candidate = store.save_candidate(candidate_payload)
    first = store.save_source({**source_payload, "candidate_id": candidate.candidate_id, "url": "https://example.test/a"})
    changed_content = "A revised permitted excerpt with a material delta."
    revised = store.save_source(
        {
            **source_payload,
            "candidate_id": candidate.candidate_id,
            "url": "https://example.test/a",
            "content": changed_content,
            "content_hash": hashlib.sha256(changed_content.encode()).hexdigest(),
        }
    )

    assert revised.source_id != first.source_id
    assert first.content == source_payload["content"]


def test_duplicate_source_hash_returns_existing_immutable_source(
    store_factory, candidate_payload, source_payload
):
    store = store_factory()
    candidate = store.save_candidate(candidate_payload)
    payload = {**source_payload, "candidate_id": candidate.candidate_id}

    first = store.save_source(payload)
    repeated = store.save_source(payload)

    assert repeated.model_dump() == first.model_dump()


def test_bundle_rejects_passages_that_reference_another_source(
    store_factory, candidate_payload, source_payload, bundle_payload
):
    store = store_factory()
    candidate = store.save_candidate(candidate_payload)
    source = store.save_source({**source_payload, "candidate_id": candidate.candidate_id})
    payload = bundle_payload(candidate.candidate_id, source.source_id)
    payload["passages"][0]["source_id"] = "source-that-is-not-in-the-bundle"

    with pytest.raises(ValueError, match="passage source"):
        store.save_bundle(payload)


def test_successful_source_requires_nonempty_content_or_excerpt(source_payload):
    with pytest.raises(ValidationError, match="content or at least one permitted excerpt"):
        SourceRecord.model_validate(
            {**source_payload, "candidate_id": "candidate-1", "content": ""}
        )


def test_source_content_hash_must_match_the_permitted_content(source_payload):
    with pytest.raises(ValidationError, match="content_hash does not match content"):
        SourceRecord.model_validate(
            {
                **source_payload,
                "candidate_id": "candidate-1",
                "content_hash": "0" * 64,
            }
        )


def test_prepared_context_and_insight_are_immutable_and_reference_existing_records(
    store_factory, candidate_payload, source_payload, bundle_payload
):
    store = store_factory()
    candidate = store.save_candidate(candidate_payload)
    source = store.save_source({**source_payload, "candidate_id": candidate.candidate_id})
    bundle = store.save_bundle(bundle_payload(candidate.candidate_id, source.source_id))
    prepared = store.save_prepared_context(
        PreparedContext(
            candidate_id=candidate.candidate_id,
            bundle_id=bundle.bundle_id,
            question="What is the learning?",
            validation_status="valid",
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )
    insight = store.save_insight(
        InsightRevision(
            prepared_context_id=prepared.prepared_context_id,
            headline="A bounded learning",
            explanation="The source supports a limited observation.",
            actual_change="A new practice was reported.",
            why_now="The candidate was submitted now.",
            personal_relevance="It addresses the question.",
            takeaway="Try a small test.",
            claims=[{"text": "A practice was reported.", "passage_ids": ["passage-fixture-1"]}],
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )

    assert store.get_prepared_context(prepared.prepared_context_id) == prepared
    assert store.get_insight(insight.insight_id) == insight

    first_receipt = store.save_delivery_receipt(
        insight.insight_id, insight.revision, "telegram", "queued"
    )
    repeated_receipt = store.save_delivery_receipt(
        insight.insight_id, insight.revision, "telegram", "queued"
    )
    sent_receipt = store.save_delivery_receipt(
        insight.insight_id, insight.revision, "telegram", "sent"
    )
    other_channel = store.save_delivery_receipt(
        insight.insight_id, insight.revision, "discord", "queued"
    )

    assert repeated_receipt == first_receipt
    assert sent_receipt["receipt_id"] == first_receipt["receipt_id"]
    assert sent_receipt["state"] == "sent"
    assert other_channel["receipt_id"] != first_receipt["receipt_id"]


def test_review_is_idempotent_and_product_agnostic(
    store_factory, candidate_payload, source_payload, bundle_payload
):
    store = store_factory()
    candidate = store.save_candidate(candidate_payload)
    source = store.save_source({**source_payload, "candidate_id": candidate.candidate_id})
    bundle = store.save_bundle(bundle_payload(candidate.candidate_id, source.source_id))
    prepared = store.save_prepared_context(
        PreparedContext(
            candidate_id=candidate.candidate_id,
            bundle_id=bundle.bundle_id,
            question="What changed?",
            validation_status="valid",
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )
    insight = store.save_insight(
        InsightRevision(
            prepared_context_id=prepared.prepared_context_id,
            headline="A bounded learning",
            explanation="The source supports a limited observation.",
            actual_change="A practice changed.",
            why_now="A source became available.",
            personal_relevance="It addresses the question.",
            takeaway="Test the boundary.",
            claims=[{"text": "A practice changed.", "passage_ids": ["passage-fixture-1"]}],
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )

    payload = {
        "insight_id": insight.insight_id,
        "revision": insight.revision,
        "disposition": "retain",
        "note": "Useful boundary to remember.",
    }
    first, first_status = store.save_idempotent_review(
        "actor-hash", "review-1", "request-hash", payload
    )
    repeated, repeated_status = store.save_idempotent_review(
        "actor-hash", "review-1", "request-hash", payload
    )

    assert first_status == 201
    assert repeated_status == 200
    assert repeated == first
    assert store.list_reviews(insight.insight_id) == [first]
    assert "product_id" not in first.model_dump()


def test_unknown_delivery_receipt_holds_and_cannot_be_requeued(
    store_factory, candidate_payload, source_payload, bundle_payload
):
    store = store_factory()
    candidate = store.save_candidate(candidate_payload)
    source = store.save_source({**source_payload, "candidate_id": candidate.candidate_id})
    bundle = store.save_bundle(bundle_payload(candidate.candidate_id, source.source_id))
    prepared = store.save_prepared_context(PreparedContext(
        candidate_id=candidate.candidate_id, bundle_id=bundle.bundle_id,
        question="What changed?", validation_status="valid", context_revision="fixture-v1",
    ).model_dump(mode="json"))
    insight = store.save_insight(InsightRevision(
        prepared_context_id=prepared.prepared_context_id, headline="A bounded learning",
        explanation="The source supports a limited observation.", actual_change="A change.",
        why_now="It is new.", personal_relevance="It matters.", takeaway="Test it.",
        claims=[{"text": "A change.", "passage_ids": ["passage-fixture-1"]}],
        context_revision="fixture-v1",
    ).model_dump(mode="json"))

    unknown = store.save_delivery_receipt(
        insight.insight_id, insight.revision, "telegram", "unknown"
    )
    retry = store.save_delivery_receipt(
        insight.insight_id, insight.revision, "telegram", "queued"
    )

    assert retry == unknown
    assert retry["state"] == "unknown"


def test_correction_keeps_old_revision_addressable_but_replaces_current_view(
    store_factory, candidate_payload, source_payload, bundle_payload
):
    store = store_factory()
    candidate = store.save_candidate(candidate_payload)
    source = store.save_source({**source_payload, "candidate_id": candidate.candidate_id})
    bundle = store.save_bundle(bundle_payload(candidate.candidate_id, source.source_id))
    prepared = store.save_prepared_context(
        PreparedContext(
            candidate_id=candidate.candidate_id, bundle_id=bundle.bundle_id,
            question="What changed?", validation_status="valid", context_revision="fixture-v1",
        ).model_dump(mode="json")
    )
    original = store.save_insight(
        InsightRevision(
            prepared_context_id=prepared.prepared_context_id,
            headline="Original", explanation="Original.", actual_change="Original change.",
            why_now="Original now.", personal_relevance="Original relevance.",
            takeaway="Original takeaway.",
            claims=[{"text": "Original claim.", "passage_ids": ["passage-fixture-1"]}],
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )
    correction = store.save_insight(
        InsightRevision(
            prepared_context_id=prepared.prepared_context_id,
            headline="Correction", explanation="Corrected.", actual_change="Corrected change.",
            why_now="Corrected now.", personal_relevance="Corrected relevance.",
            takeaway="Corrected takeaway.",
            claims=[{"text": "Corrected claim.", "passage_ids": ["passage-fixture-1"]}],
            supersedes_insight_id=original.insight_id,
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )

    assert store.get_insight(original.insight_id) == original
    assert store.list_current_insights() == [correction]
