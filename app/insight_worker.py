"""One bounded worker tick for fixture-safe personal insight analysis."""

from typing import Literal

from app.factory import build_insight_llm_provider
from app.llm.protocol import LLMProvider
from app.models.insights import InsightJob, InsightRevision, PreparedContext
from app.services.insight_analysis import analyze_bundle
from app.storage.insight_store import InsightStore


async def process_one(store: InsightStore, llm: LLMProvider) -> InsightRevision | None:
    """Claim one job and atomically save its prepared context, insight, and completion.

    Acquisition requests are handled by the control-plane adapter. This worker
    only processes a job after a bundle has been persisted by the engine.
    """
    return await _process_claimed_job(store, store.claim_job(), llm)


async def process_backfill_one(
    store: InsightStore, backfill_id: str, llm: LLMProvider
) -> InsightRevision | None:
    """Advance only the named backfill by one bounded worker step."""
    return await _process_claimed_job(store, store.claim_backfill_job(backfill_id), llm)


async def _process_claimed_job(
    store: InsightStore, job: InsightJob | None, llm: LLMProvider
) -> InsightRevision | None:
    if job is None:
        return None
    if job.bundle_id is None:
        if job.backfill_id:
            bundle = store.prepare_backfill_evidence(job.job_id, job.lease_token or "")
            if bundle is None:
                return None
            refreshed = store.get_job(job.job_id)
            if refreshed is None:
                raise ValueError("backfill job disappeared during evidence preparation")
            job = refreshed
        else:
            store.complete_job_needs_evidence(job.job_id, job.lease_token or "")
            return None
    if job.bundle_id is None:
        store.complete_job_needs_evidence(job.job_id, job.lease_token or "")
        return None
    candidate = store.get_candidate(job.candidate_id)
    bundle = store.get_bundle(job.bundle_id)
    if candidate is None or bundle is None:
        raise ValueError("leased job references unavailable candidate or bundle")
    if not bundle.passages:
        store.complete_job_needs_evidence(job.job_id, job.lease_token or "")
        return None
    if bundle.provenance_status != "attributable":
        store.complete_job_needs_evidence(
            job.job_id, job.lease_token or "", "Attributable source provenance is required"
        )
        return None
    if bundle.freshness_status == "superseded":
        store.complete_job_no_new_learning(
            job.job_id, job.lease_token or "", "Evidence bundle is superseded"
        )
        return None
    if bundle.novelty_status == "duplicate":
        store.complete_job_no_new_learning(
            job.job_id, job.lease_token or "", "Evidence bundle duplicates prior learning"
        )
        return None
    if bundle.novelty_status == "no_material_delta":
        store.complete_job_no_new_learning(
            job.job_id, job.lease_token or "", "Evidence bundle has no material delta"
        )
        return None
    status: Literal["valid", "needs_evidence"] = "valid" if bundle.passages else "needs_evidence"
    prepared = PreparedContext(
        candidate_id=candidate.candidate_id,
        bundle_id=bundle.bundle_id,
        question=candidate.question_ids[0] if candidate.question_ids else candidate.subject,
        facts=[],
        unresolved_questions=bundle.coverage_gaps,
        validation_status=status,
        context_revision=job.context_revision,
    )
    try:
        insight = await analyze_bundle(bundle, prepared, llm)
    except Exception as exc:
        store.fail_job_retryable(job.job_id, job.lease_token or "", str(exc))
        raise
    if job.supersedes_insight_id:
        insight = insight.model_copy(update={"supersedes_insight_id": job.supersedes_insight_id})
    return store.complete_job_analysis(job.job_id, job.lease_token or "", prepared, insight)


async def process_one_oauth(store: InsightStore) -> InsightRevision | None:
    """Claim at most one learning job with the isolated OAuth provider."""
    return await process_one(store, build_insight_llm_provider())


async def run_oauth_backfill_worker_tick(
    store: InsightStore, backfill_id: str
) -> InsightRevision | None:
    """Run one OAuth analysis step for exactly one explicitly named backfill."""
    job = store.claim_backfill_job(backfill_id)
    if job is None:
        return None
    return await _process_claimed_job(store, job, build_insight_llm_provider())


async def run_oauth_worker_tick(store: InsightStore) -> InsightRevision | None:
    """Execute one bounded OAuth worker tick for an already-initialized store."""
    return await process_one_oauth(store)
