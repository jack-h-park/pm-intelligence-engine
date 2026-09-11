from datetime import UTC, datetime
from typing import Any

import pytest

from app.insight_worker import process_one
from app.storage.insight_store import StaleLease


def _job_payload(candidate_id: str) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "context_revision": "fixture-context-v1",
        "purpose": "learning",
        "bundle_id": None,
    }


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
