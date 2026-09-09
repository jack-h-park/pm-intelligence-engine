from datetime import UTC, datetime

import pytest

from app.storage.insight_store import StaleLease


def _job_payload(candidate_id: str) -> dict:
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
