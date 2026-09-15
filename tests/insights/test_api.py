import hashlib
import json

from app.api.deps import get_engine
from app.api.main import app


def _seed_insight_with_evidence(engine, candidate_payload, source_payload, bundle_payload):
    from app.models.insights import InsightRevision, PreparedContext

    candidate = engine.insight_store.save_candidate(candidate_payload)
    source = engine.insight_store.save_source(
        {**source_payload, "candidate_id": candidate.candidate_id}
    )
    bundle = engine.insight_store.save_bundle(
        bundle_payload(candidate.candidate_id, source.source_id)
    )
    prepared = engine.insight_store.save_prepared_context(
        PreparedContext(
            candidate_id=candidate.candidate_id,
            bundle_id=bundle.bundle_id,
            question="What changed?",
            validation_status="valid",
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )
    insight = engine.insight_store.save_insight(
        InsightRevision(
            prepared_context_id=prepared.prepared_context_id,
            headline="Android control",
            explanation="A bounded change.",
            actual_change="Android added a control.",
            why_now="A release documented it.",
            personal_relevance="It informs device management.",
            takeaway="Verify it.",
            claims=[{"text": "A control was added.", "passage_ids": ["passage-fixture-1"]}],
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )
    return insight, source


def _backfill_payload(revision=1):
    return {
        "base_revision": revision,
        "targets": [
            {
                "url": "https://www.group-ib.com/blog/vwork-app-cloning-gigabud-goldfactory/",
                "purpose": "primary_incident",
                "question": "What campaign facts and malware behaviour are directly observed?",
            },
            {
                "url": "https://source.android.com/docs/devices/admin/managed-profiles",
                "purpose": "platform_behavior",
                "question": "What work-profile behaviour is documented by Android?",
            },
        ],
    }


def test_evidence_backfill_api_creates_an_idempotent_product_agnostic_request(
    client, auth_headers, candidate_payload, source_payload, bundle_payload
):
    """Catches an API path that accepts caller lineage or starts product-decision state."""
    engine = app.dependency_overrides[get_engine]()
    insight, _ = _seed_insight_with_evidence(
        engine, candidate_payload, source_payload, bundle_payload
    )
    headers = {**auth_headers, "Idempotency-Key": "gigabud-backfill-api-1"}

    first = client.post(
        f"/insights/{insight.insight_id}/evidence-backfills",
        json=_backfill_payload(insight.revision),
        headers=headers,
    )
    repeated = client.post(
        f"/insights/{insight.insight_id}/evidence-backfills",
        json=_backfill_payload(insight.revision),
        headers=headers,
    )

    assert first.status_code == 201
    assert repeated.status_code == 200
    assert repeated.json() == first.json()
    assert first.json()["candidate_id"] == engine.insight_store.get_candidate(
        engine.insight_store.get_prepared_context(insight.prepared_context_id).candidate_id
    ).candidate_id
    assert first.json()["state"] == "queued"
    assert [target["url"] for target in first.json()["targets"]] == [
        "https://www.group-ib.com/blog/vwork-app-cloning-gigabud-goldfactory",
        "https://source.android.com/docs/devices/admin/managed-profiles",
    ]
    assert [target["purpose"] for target in first.json()["targets"]] == [
        "primary_incident", "platform_behavior"
    ]
    assert not {"product_id", "review_id", "decision_case_id", "delivery_receipt_id"} & set(
        first.json()
    )


def test_evidence_backfill_api_rejects_changed_replays_and_invalid_bases(
    client, auth_headers, candidate_payload, source_payload, bundle_payload
):
    """Catches a retry widening targets or a request attached to a missing/stale Insight."""
    engine = app.dependency_overrides[get_engine]()
    insight, _ = _seed_insight_with_evidence(
        engine, candidate_payload, source_payload, bundle_payload
    )
    headers = {**auth_headers, "Idempotency-Key": "gigabud-backfill-api-conflict"}
    assert client.post(
        f"/insights/{insight.insight_id}/evidence-backfills",
        json=_backfill_payload(insight.revision),
        headers=headers,
    ).status_code == 201

    changed = client.post(
        f"/insights/{insight.insight_id}/evidence-backfills",
        json={
            "base_revision": insight.revision,
            "targets": [{
                "url": "https://example.test/unapproved-expansion",
                "purpose": "independent_corroboration",
                "question": "What independently corroborates the campaign?",
            }],
        },
        headers=headers,
    )
    missing = client.post(
        "/insights/missing-insight/evidence-backfills",
        json=_backfill_payload(),
        headers={**auth_headers, "Idempotency-Key": "backfill-missing"},
    )
    stale = client.post(
        f"/insights/{insight.insight_id}/evidence-backfills",
        json=_backfill_payload(insight.revision + 1),
        headers={**auth_headers, "Idempotency-Key": "backfill-stale"},
    )
    caller_candidate = client.post(
        f"/insights/{insight.insight_id}/evidence-backfills",
        json={**_backfill_payload(insight.revision), "candidate_id": "caller-selected"},
        headers={**auth_headers, "Idempotency-Key": "backfill-caller-candidate"},
    )
    unsafe_target = client.post(
        f"/insights/{insight.insight_id}/evidence-backfills",
        json={
            "base_revision": insight.revision,
            "targets": [{
                "url": "file:///private/source",
                "purpose": "primary_incident",
                "question": "What is directly observed?",
            }],
        },
        headers={**auth_headers, "Idempotency-Key": "backfill-unsafe-target"},
    )

    assert changed.status_code == 409
    assert missing.status_code == 404
    assert stale.status_code == 404
    assert caller_candidate.status_code == 422
    assert unsafe_target.status_code == 422


def test_reused_key_with_changed_body_conflicts(client, auth_headers, candidate_payload):
    headers = {**auth_headers, "Idempotency-Key": "candidate-fixture-1"}
    first = client.post("/insight-candidates", json=candidate_payload, headers=headers)
    repeated = client.post("/insight-candidates", json=candidate_payload, headers=headers)
    changed = client.post(
        "/insight-candidates",
        json={**candidate_payload, "subject": "Different"},
        headers=headers,
    )

    assert first.status_code == 201
    assert repeated.status_code == 200
    assert repeated.json()["candidate_id"] == first.json()["candidate_id"]
    assert changed.status_code == 409


def test_intake_routes_require_bearer_authentication(client, candidate_payload):
    response = client.post(
        "/insight-candidates",
        json=candidate_payload,
        headers={"Idempotency-Key": "unauthenticated"},
    )

    assert response.status_code == 401


def test_intake_routes_require_an_idempotency_key(client, auth_headers, candidate_payload):
    response = client.post("/insight-candidates", json=candidate_payload, headers=auth_headers)

    assert response.status_code == 422


def test_source_and_bundle_intake_validate_references(
    client, auth_headers, candidate_payload, source_payload, bundle_payload
):
    candidate = client.post(
        "/insight-candidates",
        json=candidate_payload,
        headers={**auth_headers, "Idempotency-Key": "candidate-for-source"},
    )
    assert candidate.status_code == 201

    bad_source = client.post(
        "/insight-sources",
        json={**source_payload, "candidate_id": "missing-candidate"},
        headers={**auth_headers, "Idempotency-Key": "bad-source"},
    )
    assert bad_source.status_code == 404

    source = client.post(
        "/insight-sources",
        json={**source_payload, "candidate_id": candidate.json()["candidate_id"]},
        headers={**auth_headers, "Idempotency-Key": "valid-source"},
    )
    assert source.status_code == 201

    bad_bundle = client.post(
        "/insight-evidence",
        json=bundle_payload(candidate.json()["candidate_id"], "not-a-source"),
        headers={**auth_headers, "Idempotency-Key": "bad-bundle"},
    )
    assert bad_bundle.status_code == 422


def test_identical_source_submission_returns_the_existing_source(
    client, auth_headers, candidate_payload, source_payload
):
    candidate = client.post(
        "/insight-candidates",
        json=candidate_payload,
        headers={**auth_headers, "Idempotency-Key": "candidate-for-dedupe"},
    ).json()
    content = source_payload["content"]
    payload = {
        **source_payload,
        "candidate_id": candidate["candidate_id"],
        "content_hash": hashlib.sha256(content.encode()).hexdigest(),
    }

    first = client.post(
        "/insight-sources", json=payload, headers={**auth_headers, "Idempotency-Key": "source-1"}
    )
    second = client.post(
        "/insight-sources", json=payload, headers={**auth_headers, "Idempotency-Key": "source-2"}
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["source_id"] == first.json()["source_id"]


def test_novelty_lookup_returns_only_known_source_hashes(
    client, auth_headers, candidate_payload, source_payload
):
    candidate = client.post(
        "/insight-candidates",
        json=candidate_payload,
        headers={**auth_headers, "Idempotency-Key": "candidate-for-novelty"},
    ).json()
    client.post(
        "/insight-sources",
        json={**source_payload, "candidate_id": candidate["candidate_id"]},
        headers={**auth_headers, "Idempotency-Key": "source-for-novelty"},
    )

    response = client.post(
        "/insight-triage/novelty",
        json={"content_hashes": [source_payload["content_hash"], "b" * 64]},
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json() == {"known_content_hashes": [source_payload["content_hash"]]}


def test_semantic_triage_reserves_before_calling_the_model(client, auth_headers, monkeypatch):
    from config import settings

    class FixtureLLM:
        calls = 0

        async def complete(self, messages, **kwargs):
            self.calls += 1
            return json.dumps({
                "disposition": "admit", "relevance": "relevant",
                "novelty": "meaningful_delta", "reason": "New evidence.",
            })

    engine = app.dependency_overrides[get_engine]()
    engine.llm = FixtureLLM()
    monkeypatch.setattr(settings, "INTELLIGENCE_SENSING_ALLOWANCE_MICROS", 10)
    monkeypatch.setattr(settings, "INTELLIGENCE_RATE_REVISION", "fixture-rates")
    response = client.post(
        "/insight-triage",
        json={
            "question": "What changed?", "title": "Change", "content": "Evidence.",
            "operation_id": "triage-api", "policy_revision": "fixture-v1", "provider": "fixture",
            "rate_revision": "fixture-rates", "maximum_micros": 10, "actual_micros": 4,
        },
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["disposition"] == "admit"
    assert engine.insight_store.operational_summary()["cost_micros"]["finalized"] == 4

    repeated = client.post(
        "/insight-triage",
        json={
            "question": "What changed?", "title": "Change", "content": "Evidence.",
            "operation_id": "triage-api", "policy_revision": "fixture-v1", "provider": "fixture",
            "rate_revision": "fixture-rates", "maximum_micros": 10, "actual_micros": 4,
        },
        headers=auth_headers,
    )
    assert repeated.json() == response.json()
    assert engine.llm.calls == 1


def test_job_intake_is_idempotent_and_requires_an_existing_candidate(
    client, auth_headers, candidate_payload
):
    candidate = client.post(
        "/insight-candidates",
        json=candidate_payload,
        headers={**auth_headers, "Idempotency-Key": "candidate-for-job"},
    ).json()
    payload = {
        "candidate_id": candidate["candidate_id"],
        "context_revision": "fixture-context-v1",
        "purpose": "learning",
        "bundle_id": None,
    }
    headers = {**auth_headers, "Idempotency-Key": "job-fixture-1"}

    first = client.post("/insight-jobs", json=payload, headers=headers)
    repeated = client.post("/insight-jobs", json=payload, headers=headers)
    detail = client.get(f"/insight-jobs/{first.json()['job_id']}", headers=auth_headers)

    assert first.status_code == 202
    assert repeated.status_code == 200
    assert repeated.json()["job_id"] == first.json()["job_id"]
    assert detail.json()["state"] == "queued"


def test_budget_denial_never_creates_a_paid_reservation(client, auth_headers):
    response = client.post(
        "/insight-budget/reservations",
        json={
            "operation_id": "fixture-paid-operation",
            "operation_type": "analysis",
            "policy_revision": "fixture-policy-v1",
            "provider": "fixture-provider",
            "rate_revision": "fixture-rates-v1",
            "maximum_micros": 1,
            "allowance_class": "sensing",
        },
        headers={**auth_headers, "Idempotency-Key": "budget-denied"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "budget_denied"


def test_insight_evidence_returns_only_cited_passages_and_source_metadata(
    client, auth_headers, candidate_payload, source_payload, bundle_payload
):
    def bundle_with_uncited_passage(candidate_id, source_id):
        payload = bundle_payload(candidate_id, source_id)
        payload["passages"].append(
            {
                "passage_id": "passage-fixture-uncited",
                "source_id": source_id,
                "locator": "uncited statement",
                "text": "This uncited passage must remain private.",
                "role": "enrichment",
            }
        )
        return payload

    engine = app.dependency_overrides[get_engine]()
    insight, source = _seed_insight_with_evidence(
        engine, candidate_payload, source_payload, bundle_with_uncited_passage
    )

    response = client.get(
        f"/insights/{insight.insight_id}/evidence?revision=1", headers=auth_headers
    )

    assert response.status_code == 200
    assert response.json() == {
        "insight_id": insight.insight_id,
        "revision": 1,
        "sources": [
            {
                "source_id": source.source_id,
                "origin": "user_supplied",
                "acquisition_status": "ok",
                "retrieved_at": "2026-09-08T00:00:00Z",
                "url": None,
                "legacy_reference": None,
            }
        ],
        "passages": [
            {
                "passage_id": "passage-fixture-1",
                "source_id": source.source_id,
                "locator": "user statement",
                "text": "The user observed selected intent sharing in a managed work profile.",
                "role": "seed",
            }
        ],
        "claim_passage_links": [{"claim_index": 0, "passage_ids": ["passage-fixture-1"]}],
        "coarse_evidence": True,
    }


def test_insight_evidence_rejects_a_stale_revision(
    client, auth_headers, candidate_payload, source_payload, bundle_payload
):
    engine = app.dependency_overrides[get_engine]()
    insight, _ = _seed_insight_with_evidence(
        engine, candidate_payload, source_payload, bundle_payload
    )

    response = client.get(
        f"/insights/{insight.insight_id}/evidence?revision=2", headers=auth_headers
    )

    assert response.status_code == 409


def test_authenticated_insight_search_returns_stored_revision(
    client, auth_headers, candidate_payload, source_payload, bundle_payload, monkeypatch
):
    from app.models.insights import InsightRevision, PreparedContext
    from config import settings

    engine = app.dependency_overrides[get_engine]()
    candidate = engine.insight_store.save_candidate(candidate_payload)
    source = engine.insight_store.save_source(
        {**source_payload, "candidate_id": candidate.candidate_id}
    )
    bundle = engine.insight_store.save_bundle(
        bundle_payload(candidate.candidate_id, source.source_id)
    )
    prepared = engine.insight_store.save_prepared_context(
        PreparedContext(
            candidate_id=candidate.candidate_id,
            bundle_id=bundle.bundle_id,
            question="What changed?",
            validation_status="valid",
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )
    insight = engine.insight_store.save_insight(
        InsightRevision(
            prepared_context_id=prepared.prepared_context_id,
            headline="Android control",
            explanation="A bounded change.",
            actual_change="Android added a control.",
            why_now="A release documented it.",
            personal_relevance="It informs device management.",
            takeaway="Verify it.",
            claims=[{"text": "A control was added.", "passage_ids": ["passage-fixture-1"]}],
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )

    response = client.get("/insights/search?q=Android", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["items"][0]["insight_id"] == insight.insight_id

    detail = client.get(f"/insights/{insight.insight_id}", headers=auth_headers)
    assert detail.status_code == 200
    assert detail.json()["takeaway"] == "Verify it."

    operations = client.get("/insight-operations", headers=auth_headers)
    assert operations.status_code == 200
    assert operations.json()["candidates"] == 1
    assert operations.json()["cost_micros"] == {
        "reserved": 0, "finalized": 0, "unknown": 0,
    }
    assert operations.json()["feedback"] == {"recorded": 0, "unknown": 0}

    suppressed = client.post(
        f"/insights/{insight.insight_id}/delivery-receipts",
        json={"revision": insight.revision, "channel": "telegram", "state": "queued"},
        headers=auth_headers,
    )
    assert suppressed.status_code == 409
    monkeypatch.setattr(settings, "INTELLIGENCE_MODE", "insights")
    receipt = client.post(
        f"/insights/{insight.insight_id}/delivery-receipts",
        json={"revision": insight.revision, "channel": "telegram", "state": "queued"},
        headers=auth_headers,
    )
    repeated = client.post(
        f"/insights/{insight.insight_id}/delivery-receipts",
        json={"revision": insight.revision, "channel": "telegram", "state": "queued"},
        headers=auth_headers,
    )
    assert receipt.status_code == 201
    assert repeated.json() == receipt.json()

    feedback = client.post(
        f"/insights/{insight.insight_id}/feedback",
        json={"revision": insight.revision, "label": "useful"},
        headers=auth_headers,
    )
    assert feedback.status_code == 200
    assert feedback.json()["label"] == "useful"
    # Silence is not invented as a negative or positive feedback record.
    assert client.get("/insight-operations", headers=auth_headers).json()["feedback"] == {
        "recorded": 1, "unknown": 0,
    }


def test_authenticated_insight_listing_returns_product_agnostic_revision(
    client, auth_headers, candidate_payload, source_payload, bundle_payload
):
    from app.models.insights import InsightRevision, PreparedContext

    engine = app.dependency_overrides[get_engine]()
    candidate = engine.insight_store.save_candidate(candidate_payload)
    source = engine.insight_store.save_source(
        {**source_payload, "candidate_id": candidate.candidate_id}
    )
    bundle = engine.insight_store.save_bundle(
        bundle_payload(candidate.candidate_id, source.source_id)
    )
    prepared = engine.insight_store.save_prepared_context(
        PreparedContext(
            candidate_id=candidate.candidate_id,
            bundle_id=bundle.bundle_id,
            question="What changed?",
            validation_status="valid",
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )
    insight = engine.insight_store.save_insight(
        InsightRevision(
            prepared_context_id=prepared.prepared_context_id,
            headline="Android work-profile learning",
            explanation="A bounded learning observation.",
            actual_change="A work profile changed the observed boundary.",
            why_now="New evidence is available.",
            personal_relevance="It informs enterprise mobile security.",
            takeaway="Review profile-boundary controls.",
            claims=[{"text": "The boundary changed.", "passage_ids": ["passage-fixture-1"]}],
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )

    response = client.get("/insights?limit=10", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["items"][0]["insight_id"] == insight.insight_id
    assert "product_id" not in response.json()["items"][0]


def test_insight_review_is_authenticated_idempotent_and_does_not_start_a_decision(
    client, auth_headers, candidate_payload, source_payload, bundle_payload
):
    from app.models.insights import InsightRevision, PreparedContext

    engine = app.dependency_overrides[get_engine]()
    candidate = engine.insight_store.save_candidate(candidate_payload)
    source = engine.insight_store.save_source(
        {**source_payload, "candidate_id": candidate.candidate_id}
    )
    bundle = engine.insight_store.save_bundle(
        bundle_payload(candidate.candidate_id, source.source_id)
    )
    prepared = engine.insight_store.save_prepared_context(
        PreparedContext(
            candidate_id=candidate.candidate_id,
            bundle_id=bundle.bundle_id,
            question="What changed?",
            validation_status="valid",
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )
    insight = engine.insight_store.save_insight(
        InsightRevision(
            prepared_context_id=prepared.prepared_context_id,
            headline="Android work-profile learning",
            explanation="A bounded learning observation.",
            actual_change="A work profile changed the observed boundary.",
            why_now="New evidence is available.",
            personal_relevance="It informs enterprise mobile security.",
            takeaway="Review profile-boundary controls.",
            claims=[{"text": "The boundary changed.", "passage_ids": ["passage-fixture-1"]}],
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )
    review_payload = {
        "revision": insight.revision,
        "disposition": "retain",
        "note": "Useful boundary to remember.",
    }
    headers = {**auth_headers, "Idempotency-Key": "insight-review-1"}

    first = client.post(
        f"/insights/{insight.insight_id}/reviews", json=review_payload, headers=headers
    )
    repeated = client.post(
        f"/insights/{insight.insight_id}/reviews", json=review_payload, headers=headers
    )
    listed = client.get(f"/insights/{insight.insight_id}/reviews", headers=auth_headers)

    assert engine.store is None
    assert first.status_code == 201
    assert repeated.status_code == 200
    assert repeated.json() == first.json()
    assert listed.status_code == 200
    assert listed.json()["items"] == [first.json()]
    assert "product_id" not in first.json()
    assert "insight-test-token" not in first.text

    unauthenticated = client.post(
        f"/insights/{insight.insight_id}/reviews",
        json=review_payload,
        headers={"Idempotency-Key": "review-no-auth"},
    )
    stale = client.post(
        f"/insights/{insight.insight_id}/reviews",
        json={**review_payload, "revision": insight.revision + 1},
        headers={**auth_headers, "Idempotency-Key": "review-stale"},
    )
    product_attempt = client.post(
        f"/insights/{insight.insight_id}/reviews",
        json={**review_payload, "product_id": "samsung-knox-mtd"},
        headers={**auth_headers, "Idempotency-Key": "review-product-attempt"},
    )

    assert unauthenticated.status_code == 401
    assert stale.status_code == 409
    assert product_attempt.status_code == 422
