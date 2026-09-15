import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import app.insight_worker as insight_worker
from app.insight_worker import process_backfill_one, process_one, run_oauth_backfill_worker_tick
from app.models.insights import EvidenceBackfillTarget, InsightRevision, PreparedContext
from app.storage.insight_store import StaleLease

GIGABUD_BACKFILL_FIXTURE = Path(__file__).parent / "fixtures" / "gigabud-backfill.json"


def test_gigabud_backfill_fixture_locks_review_scope_without_a_product():
    """The non-live pilot fixture must not silently widen its review scope."""
    fixture = json.loads(GIGABUD_BACKFILL_FIXTURE.read_text())

    assert fixture["base_insight_id"] == "3ddb6cba-6932-48f1-8076-baf0b397c191"
    assert fixture["base_revision"] == 1
    assert "product_id" not in fixture
    assert [(source["role"], source["url"]) for source in fixture["sources"]] == [
        (
            "primary_incident",
            "https://www.group-ib.com/blog/vwork-app-cloning-gigabud-goldfactory/",
        ),
        (
            "platform_behavior",
            "https://source.android.com/docs/devices/admin/managed-profiles",
        ),
    ]
    assert all(source["passages"] for source in fixture["sources"])


def _job_payload(candidate_id: str) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "context_revision": "fixture-context-v1",
        "purpose": "learning",
        "bundle_id": None,
    }


def _save_base_insight(
    store, candidate_payload, source_payload, bundle_payload, *, with_prior_enrichment=False
):
    candidate = store.save_candidate(candidate_payload)
    source = store.save_source({**source_payload, "candidate_id": candidate.candidate_id})
    payload = bundle_payload(candidate.candidate_id, source.source_id)
    if with_prior_enrichment:
        import hashlib

        content = "A prior bounded enrichment remains part of the base Insight."
        enrichment = store.save_source({
            **source_payload,
            "candidate_id": candidate.candidate_id,
            "content": content,
            "content_hash": hashlib.sha256(content.encode()).hexdigest(),
        })
        payload["source_ids"].append(enrichment.source_id)
        payload["passages"].append({
            "passage_id": "passage-fixture-prior-enrichment",
            "source_id": enrichment.source_id,
            "locator": "prior enrichment",
            "text": content,
            "role": "enrichment",
        })
    bundle = store.save_bundle(payload)
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
            headline="Original learning",
            explanation="Original bounded evidence.",
            actual_change="A source was supplied.",
            why_now="The original job was queued.",
            personal_relevance="It addresses the question.",
            takeaway="Keep the original evidence.",
            claims=[{"text": "A source was supplied.", "passage_ids": ["passage-fixture-1"]}],
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )
    return candidate, insight


@pytest.mark.asyncio
async def test_scoped_backfill_tick_never_claims_an_unrelated_learning_job(
    store_factory, candidate_payload, source_payload, bundle_payload
):
    """Catches a bounded runner silently consuming the generic learning queue."""
    store = store_factory()
    candidate, base = _save_base_insight(store, candidate_payload, source_payload, bundle_payload)
    backfill, _ = store.create_idempotent_backfill(
        "fixture-reviewer",
        "scoped-backfill-tick",
        "request-hash-scoped-backfill-tick",
        insight_id=base.insight_id,
        base_revision=base.revision,
        targets=[EvidenceBackfillTarget(
            url="https://www.group-ib.com/blog/vwork-app-cloning-gigabud-goldfactory/",
            purpose="primary_incident",
            question="What campaign facts are directly observed?",
        )],
    )
    unrelated = store.create_job(_job_payload(candidate.candidate_id))

    assert await process_backfill_one(store, backfill.backfill_id, object()) is None
    assert store.get_backfill(backfill.backfill_id).state == "waiting_research"
    assert store.get_job(unrelated.job_id).state == "queued"


@pytest.mark.asyncio
async def test_terminal_or_waiting_scoped_tick_does_not_initialize_an_oauth_provider(
    store_factory, candidate_payload, source_payload, bundle_payload, monkeypatch
):
    """Catches a no-op runner spending OAuth setup before proving a lease exists."""
    store = store_factory()
    _, base = _save_base_insight(store, candidate_payload, source_payload, bundle_payload)
    backfill, _ = store.create_idempotent_backfill(
        "fixture-reviewer",
        "scoped-backfill-noop",
        "request-hash-scoped-backfill-noop",
        insight_id=base.insight_id,
        base_revision=base.revision,
        targets=[EvidenceBackfillTarget(
            url="https://www.group-ib.com/blog/vwork-app-cloning-gigabud-goldfactory/",
            purpose="primary_incident",
            question="What campaign facts are directly observed?",
        )],
    )
    assert await process_backfill_one(store, backfill.backfill_id, object()) is None
    monkeypatch.setattr(
        insight_worker,
        "build_insight_llm_provider",
        lambda: (_ for _ in ()).throw(AssertionError("provider must not initialize")),
    )

    assert await run_oauth_backfill_worker_tick(store, backfill.backfill_id) is None


def test_scoped_backfill_claim_recovers_only_its_own_expired_lease(
    store_factory, candidate_payload, source_payload, bundle_payload
):
    """Catches a crashed scoped tick leaving its approved backfill permanently running."""
    store = store_factory()
    _, base = _save_base_insight(store, candidate_payload, source_payload, bundle_payload)
    backfill, _ = store.create_idempotent_backfill(
        "fixture-reviewer",
        "scoped-backfill-expired-lease",
        "request-hash-scoped-backfill-expired-lease",
        insight_id=base.insight_id,
        base_revision=base.revision,
        targets=[EvidenceBackfillTarget(
            url="https://www.group-ib.com/blog/vwork-app-cloning-gigabud-goldfactory/",
            purpose="primary_incident",
            question="What campaign facts are directly observed?",
        )],
    )
    started = datetime(2026, 9, 15, tzinfo=UTC)
    first = store.claim_backfill_job(backfill.backfill_id, now=started)

    recovered = store.claim_backfill_job(backfill.backfill_id, now=started + timedelta(seconds=121))

    assert first is not None
    assert recovered is not None
    assert recovered.job_id == first.job_id
    assert recovered.attempt_count == 2


@pytest.mark.asyncio
async def test_backfill_worker_uses_allowlisted_results_to_create_a_superseding_insight(
    store_factory, candidate_payload, source_payload, bundle_payload
):
    """Catches a backfill that skips research, replaces its base, or lacks provenance linkage."""
    class FixtureLLM:
        async def complete(self, messages, **kwargs):
            import json

            evidence = json.loads(messages[1]["content"])["evidence"]
            return json.dumps({
                "headline": "Enriched learning",
                "explanation": "The original source is supplemented by two bounded reports.",
                "actual_change": "Additional incident and platform evidence is available.",
                "why_now": "An operator requested a bounded evidence backfill.",
                "personal_relevance": "It improves the reviewable learning record.",
                "takeaway": "Review incident and platform claims separately.",
                "claims": [
                    {
                        "text": "The incident report describes campaign behavior.",
                        "passage_ids": [evidence[-2]["passage_id"]],
                    },
                    {
                        "text": "Android documents managed-profile behavior.",
                        "passage_ids": [evidence[-1]["passage_id"]],
                    },
                ],
                "uncertainties": ["No product relevance is inferred from the sources."],
            })

    store = store_factory()
    candidate, base = _save_base_insight(
        store, candidate_payload, source_payload, bundle_payload, with_prior_enrichment=True
    )
    targets = [
        EvidenceBackfillTarget(
            url="https://www.group-ib.com/blog/vwork-app-cloning-gigabud-goldfactory/",
            purpose="primary_incident",
            question="What campaign facts are directly observed?",
        ),
        EvidenceBackfillTarget(
            url="https://source.android.com/docs/devices/admin/managed-profiles",
            purpose="platform_behavior",
            question="What managed-profile behavior is documented?",
        ),
    ]
    backfill, _ = store.create_idempotent_backfill(
        "fixture-reviewer", "backfill-worker", "request-hash-worker",
        insight_id=base.insight_id, base_revision=base.revision, targets=targets,
    )

    assert backfill.candidate_id == candidate.candidate_id
    assert backfill.job_id is not None
    assert await process_one(store, FixtureLLM()) is None
    waiting = store.get_backfill(backfill.backfill_id)
    research = store.get_research_request(waiting.research_request_id)
    assert waiting.state == "waiting_research"
    assert research.targets == [target.url for target in targets]

    claimed = store.claim_research("fixture-adapter")
    group_ib = "Group-IB directly observed the Gigabud campaign behavior."
    aosp = "Android documents managed profiles as a separate work container."
    store.submit_research_results(
        research.research_request_id,
        claimed.lease_token,
        [
            {
                "target": targets[0].url,
                "content": group_ib,
                "content_hash": __import__("hashlib").sha256(group_ib.encode()).hexdigest(),
                "acquisition_status": "ok",
            },
            {
                "target": targets[1].url,
                "content": aosp,
                "content_hash": __import__("hashlib").sha256(aosp.encode()).hexdigest(),
                "acquisition_status": "ok",
            },
        ],
        [],
    )

    completed = await process_one(store, FixtureLLM())

    assert completed is not None
    assert completed.supersedes_insight_id == base.insight_id
    assert store.get_insight(base.insight_id) == base
    saved_backfill = store.get_backfill(backfill.backfill_id)
    assert saved_backfill.state == "complete"
    assert saved_backfill.resulting_insight_id == completed.insight_id
    bundle = store.get_bundle(saved_backfill.bundle_id)
    base_bundle = store.get_bundle(store.get_prepared_context(base.prepared_context_id).bundle_id)
    assert len(bundle.source_ids) == 4
    assert set(base_bundle.source_ids) <= set(bundle.source_ids)
    assert all(claim.passage_ids for claim in completed.claims)


@pytest.mark.asyncio
async def test_backfill_with_no_usable_enrichment_completes_quietly_as_needs_evidence(
    store_factory, candidate_payload, source_payload, bundle_payload
):
    """Catches failed acquisition replacing the base Insight or creating a partial learning."""
    store = store_factory()
    _, base = _save_base_insight(store, candidate_payload, source_payload, bundle_payload)
    target = EvidenceBackfillTarget(
        url="https://www.group-ib.com/blog/vwork-app-cloning-gigabud-goldfactory/",
        purpose="primary_incident",
        question="What campaign facts are directly observed?",
    )
    backfill, _ = store.create_idempotent_backfill(
        "fixture-reviewer", "backfill-no-evidence", "request-hash-no-evidence",
        insight_id=base.insight_id, base_revision=base.revision, targets=[target],
    )

    assert await process_one(store, object()) is None
    waiting = store.get_backfill(backfill.backfill_id)
    research = store.get_research_request(waiting.research_request_id)
    claimed = store.claim_research("fixture-adapter")
    store.submit_research_results(
        research.research_request_id,
        claimed.lease_token,
        [],
        [{"target": target.url, "status": "fetch_failed"}],
    )

    assert await process_one(store, object()) is None
    saved_backfill = store.get_backfill(backfill.backfill_id)
    assert saved_backfill.state == "needs_evidence"
    assert store.get_insight(base.insight_id) == base
    assert store.list_insights() == [base]


@pytest.mark.asyncio
async def test_fallback_backfill_evidence_marks_the_backfill_needs_evidence(
    store_factory, candidate_payload, source_payload, bundle_payload
):
    """Catches a non-attributable fallback leaving an operator request stuck analyzing."""
    store = store_factory()
    _, base = _save_base_insight(store, candidate_payload, source_payload, bundle_payload)
    target = EvidenceBackfillTarget(
        url="https://www.group-ib.com/blog/vwork-app-cloning-gigabud-goldfactory/",
        purpose="primary_incident",
        question="What campaign facts are directly observed?",
    )
    backfill, _ = store.create_idempotent_backfill(
        "fixture-reviewer", "backfill-fallback", "request-hash-fallback",
        insight_id=base.insight_id, base_revision=base.revision, targets=[target],
    )
    assert await process_one(store, object()) is None
    waiting = store.get_backfill(backfill.backfill_id)
    research = store.get_research_request(waiting.research_request_id)
    claimed = store.claim_research("fixture-adapter")
    fallback_body = "A fallback summary without direct attributable source material."
    store.submit_research_results(
        research.research_request_id,
        claimed.lease_token,
        [{
            "target": target.url,
            "content": fallback_body,
            "content_hash": __import__("hashlib").sha256(fallback_body.encode()).hexdigest(),
            "acquisition_status": "fallback_summary",
        }],
        [],
    )

    assert await process_one(store, object()) is None
    assert store.get_job(backfill.job_id).completion_disposition == "needs_evidence"
    assert store.get_backfill(backfill.backfill_id).state == "needs_evidence"


def test_backfill_evidence_preparation_rejects_an_expired_job_lease(
    store_factory, candidate_payload, source_payload, bundle_payload
):
    """Catches an expired worker claiming new research after its authority ended."""
    store = store_factory()
    _, base = _save_base_insight(store, candidate_payload, source_payload, bundle_payload)
    target = EvidenceBackfillTarget(
        url="https://www.group-ib.com/blog/vwork-app-cloning-gigabud-goldfactory/",
        purpose="primary_incident",
        question="What campaign facts are directly observed?",
    )
    backfill, _ = store.create_idempotent_backfill(
        "fixture-reviewer", "backfill-expired-lease", "request-hash-expired-lease",
        insight_id=base.insight_id, base_revision=base.revision, targets=[target],
    )
    claimed = store.claim_job(now=datetime(2026, 9, 8, tzinfo=UTC))

    with pytest.raises(StaleLease):
        store.prepare_backfill_evidence(
            backfill.job_id,
            claimed.lease_token,
            now=datetime(2026, 9, 8, 0, 3, tzinfo=UTC),
        )


def test_stale_worker_lease_cannot_create_research_request(store_factory, candidate_payload):
    store = store_factory()
    candidate = store.save_candidate(candidate_payload)
    job = store.create_job(_job_payload(candidate.candidate_id))
    first = store.claim_job(now=datetime(2026, 9, 8, tzinfo=UTC))
    second = store.claim_job(now=datetime(2026, 9, 8, 0, 3, tzinfo=UTC))

    assert first is not None
    assert second is not None
    assert second.lease_token != first.lease_token
    with pytest.raises(StaleLease):
        store.create_research_request(
            job.job_id,
            first.lease_token,
            targets=["https://example.test/primary"],
            questions=["What changed?"],
            maximum_fetch_count=1,
            now=datetime(2026, 9, 8, 0, 3, tzinfo=UTC),
        )


def test_expired_research_resumes_job_with_explicit_evidence_gap(store_factory, candidate_payload):
    store = store_factory()
    candidate = store.save_candidate(candidate_payload)
    job = store.create_job(_job_payload(candidate.candidate_id))
    claimed = store.claim_job(now=datetime(2026, 9, 8, tzinfo=UTC))
    request = store.create_research_request(
        job.job_id,
        claimed.lease_token,
        targets=["https://example.test/primary"],
        questions=["What changed?"],
        maximum_fetch_count=1,
        now=datetime(2026, 9, 8, tzinfo=UTC),
    )

    store.expire_research_requests(now=datetime(2026, 9, 8, 0, 11, tzinfo=UTC))
    restarted = store_factory()

    resumed = restarted.get_job(job.job_id)
    assert resumed.state == "queued"
    assert "Research request expired" in resumed.error
    assert restarted.get_research_request(request.research_request_id).state == "expired"


def test_duplicate_research_result_is_safe_and_resumes_once(store_factory, candidate_payload):
    store = store_factory()
    candidate = store.save_candidate(candidate_payload)
    job = store.create_job(_job_payload(candidate.candidate_id))
    claimed = store.claim_job(now=datetime(2026, 9, 8, tzinfo=UTC))
    request = store.create_research_request(
        job.job_id,
        claimed.lease_token,
        targets=["https://example.test/primary"],
        questions=["What changed?"],
        maximum_fetch_count=1,
        now=datetime(2026, 9, 8, tzinfo=UTC),
    )
    research = store.claim_research("fixture-adapter", now=datetime(2026, 9, 8, tzinfo=UTC))
    result = {"content_hash": "a" * 64, "status": "ok"}

    first = store.submit_research_results(
        request.research_request_id,
        research.lease_token,
        [result],
        [],
        now=datetime(2026, 9, 8, tzinfo=UTC),
    )
    repeated = store.submit_research_results(
        request.research_request_id,
        research.lease_token,
        [result],
        [],
        now=datetime(2026, 9, 8, tzinfo=UTC),
    )

    assert first.state == "complete"
    assert repeated.state == "complete"
    assert store.get_job(job.job_id).state == "queued"


@pytest.mark.asyncio
async def test_worker_completes_a_leased_job_with_prepared_context_and_insight(
    store_factory, candidate_payload, source_payload, bundle_payload
):
    class FixtureLLM:
        async def complete(self, messages, **kwargs):
            return """{
              "headline": "A fixture insight", "explanation": "Bounded evidence.",
              "actual_change": "A source was supplied.", "why_now": "The job is queued.",
              "personal_relevance": "It answers the question.", "takeaway": "Test it.",
              "claims": [{
                "text": "The source was supplied.",
                "passage_ids": ["passage-fixture-1"]
              }],
              "uncertainties": []
            }"""

    store = store_factory()
    candidate = store.save_candidate(candidate_payload)
    source = store.save_source({**source_payload, "candidate_id": candidate.candidate_id})
    bundle = store.save_bundle(bundle_payload(candidate.candidate_id, source.source_id))
    job = store.create_job({**_job_payload(candidate.candidate_id), "bundle_id": bundle.bundle_id})

    completed = await process_one(store, FixtureLLM())

    assert completed is not None
    assert store.get_job(job.job_id).state == "complete"
    assert store.get_prepared_context(completed.prepared_context_id)
    assert store.get_insight(completed.insight_id) == completed


@pytest.mark.asyncio
async def test_worker_marks_an_empty_bundle_as_needing_evidence(store_factory, candidate_payload):
    store = store_factory()
    candidate = store.save_candidate(candidate_payload)
    job = store.create_job(_job_payload(candidate.candidate_id))

    completed = await process_one(store, object())

    assert completed is None
    saved = store.get_job(job.job_id)
    assert saved.state == "complete"
    assert saved.completion_disposition == "needs_evidence"


@pytest.mark.asyncio
async def test_worker_requires_attributable_provenance_before_creating_an_insight(
    store_factory, candidate_payload, source_payload, bundle_payload
):
    store = store_factory()
    candidate = store.save_candidate(candidate_payload)
    source = store.save_source({**source_payload, "candidate_id": candidate.candidate_id})
    bundle = store.save_bundle(
        {
            **bundle_payload(candidate.candidate_id, source.source_id),
            "provenance_status": "missing",
        }
    )
    job = store.create_job({**_job_payload(candidate.candidate_id), "bundle_id": bundle.bundle_id})

    completed = await process_one(store, object())

    assert completed is None
    saved = store.get_job(job.job_id)
    assert saved.completion_disposition == "needs_evidence"
    assert "provenance" in (saved.error or "").lower()
    assert store.list_insights() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bundle_patch", "expected_reason"),
    [
        ({"freshness_status": "superseded"}, "superseded"),
        ({"novelty_status": "duplicate"}, "duplicate"),
        ({"novelty_status": "no_material_delta"}, "material delta"),
    ],
)
async def test_worker_does_not_create_an_insight_for_explicit_no_new_learning(
    store_factory, candidate_payload, source_payload, bundle_payload, bundle_patch, expected_reason
):
    store = store_factory()
    candidate = store.save_candidate(candidate_payload)
    source = store.save_source({**source_payload, "candidate_id": candidate.candidate_id})
    bundle = store.save_bundle(
        {**bundle_payload(candidate.candidate_id, source.source_id), **bundle_patch}
    )
    job = store.create_job({**_job_payload(candidate.candidate_id), "bundle_id": bundle.bundle_id})

    completed = await process_one(store, object())

    assert completed is None
    saved = store.get_job(job.job_id)
    assert saved.completion_disposition == "no_new_learning"
    assert expected_reason in (saved.error or "").lower()
    assert store.list_insights() == []


@pytest.mark.asyncio
async def test_oauth_worker_tick_completes_one_learning_job(
    monkeypatch, store_factory, candidate_payload, source_payload, bundle_payload
):
    from app.insight_worker import process_one_oauth

    class OAuthFixture:
        async def complete(self, messages, **kwargs):
            return """{"headline":"OAuth insight","explanation":"Evidence-backed.","actual_change":"A source changed.","why_now":"New evidence.","personal_relevance":"Relevant.","takeaway":"Review it.","claims":[{"text":"A source changed.","passage_ids":["passage-fixture-1"]}]}"""  # noqa: E501

    monkeypatch.setattr("app.insight_worker.build_insight_llm_provider", lambda: OAuthFixture())
    store = store_factory()
    candidate = store.save_candidate(candidate_payload)
    source = store.save_source({**source_payload, "candidate_id": candidate.candidate_id})
    bundle = store.save_bundle(bundle_payload(candidate.candidate_id, source.source_id))
    job = store.create_job({**_job_payload(candidate.candidate_id), "bundle_id": bundle.bundle_id})

    completed = await process_one_oauth(store)

    assert completed is not None
    assert store.get_job(job.job_id).state == "complete"


@pytest.mark.asyncio
async def test_oauth_worker_tick_returns_none_when_queue_is_empty(monkeypatch, store_factory):
    from app.insight_worker import run_oauth_worker_tick

    monkeypatch.setattr("app.insight_worker.build_insight_llm_provider", object)

    assert await run_oauth_worker_tick(store_factory()) is None


@pytest.mark.asyncio
async def test_worker_returns_oauth_failure_to_retryable_queue(
    store_factory, candidate_payload, source_payload, bundle_payload
):
    class FailingLLM:
        async def complete(self, messages, **kwargs):
            raise ValueError("OAuth provider returned malformed JSON")

    store = store_factory()
    candidate = store.save_candidate(candidate_payload)
    source = store.save_source({**source_payload, "candidate_id": candidate.candidate_id})
    bundle = store.save_bundle(bundle_payload(candidate.candidate_id, source.source_id))
    job = store.create_job({**_job_payload(candidate.candidate_id), "bundle_id": bundle.bundle_id})

    with pytest.raises(ValueError, match="malformed JSON"):
        await process_one(store, FailingLLM())

    saved = store.get_job(job.job_id)
    assert saved.state == "retryable_failed"
    assert saved.lease_token is None
    assert saved.lease_expires_at is None
    assert saved.next_attempt_at is not None
    assert "malformed JSON" in saved.error
