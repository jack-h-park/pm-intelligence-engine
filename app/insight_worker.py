"""One bounded worker tick for fixture-safe personal insight analysis."""

from app.factory import build_insight_llm_provider
from app.llm.protocol import LLMProvider
from app.models.insights import InsightJob, InsightRevision
from app.services.insight_analysis import analyze_bundle
from app.services.insight_budget import BudgetPolicy, BudgetService
from app.services.insight_context import load_prepared_context
from app.services.insight_knowledge_verdict import judge_knowledge
from app.storage.insight_store import InsightStore
from config import settings


async def process_one(
    store: InsightStore, llm: LLMProvider, *, decision_context_root: str | None = None
) -> InsightRevision | None:
    """Claim one job and atomically save its prepared context, insight, and completion.

    Acquisition requests are handled by the control-plane adapter. This worker
    only processes a job after a bundle has been persisted by the engine.
    """
    return await _process_claimed_job(
        store, store.claim_job(), llm, decision_context_root=decision_context_root
    )


async def process_backfill_one(
    store: InsightStore, backfill_id: str, llm: LLMProvider
) -> InsightRevision | None:
    """Advance only the named backfill by one bounded worker step."""
    return await _process_claimed_job(store, store.claim_backfill_job(backfill_id), llm)


async def process_scoped_candidate_one(
    store: InsightStore, candidate_id: str, llm: LLMProvider
) -> InsightRevision | None:
    """Advance one named Candidate without touching the generic job queue."""
    store.ensure_scoped_candidate_job(candidate_id)
    return await _process_claimed_job(store, store.claim_scoped_candidate_job(candidate_id), llm)


async def _process_claimed_job(
    store: InsightStore, job: InsightJob | None, llm: LLMProvider,
    *, decision_context_root: str | None = None,
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
    try:
        prepared = load_prepared_context(
            candidate, bundle,
            decision_context_root if decision_context_root is not None
            else settings.DECISION_CONTEXT_ROOT,
        )
        insight = await analyze_bundle(bundle, prepared, llm)
    except Exception as exc:
        store.fail_job_retryable(job.job_id, job.lease_token or "", str(exc))
        raise
    if job.supersedes_insight_id:
        insight = insight.model_copy(update={"supersedes_insight_id": job.supersedes_insight_id})
    insight = await _attach_knowledge_verdict(store, job, insight, llm)
    return store.complete_job_analysis(job.job_id, job.lease_token or "", prepared, insight)


async def _attach_knowledge_verdict(
    store: InsightStore, job: InsightJob, insight: InsightRevision, llm: LLMProvider
) -> InsightRevision:
    """Attach a non-blocking, Engine-owned reuse judgment before immutable persistence."""
    related = [
        {
            "insight_id": prior.insight_id,
            "headline": prior.headline,
            "explanation": prior.explanation,
            "takeaway": prior.takeaway,
            "related_insight_ids": prior.related_insight_ids,
            "supersedes_insight_id": prior.supersedes_insight_id,
        }
        for prior in store.list_insights()
    ]
    verdict = await judge_knowledge(
        insight=insight.model_dump(mode="json"),
        related_insights=related,
        rubric_path=settings.KNOWLEDGE_RUBRIC_PATH,
        llm=llm,
        model=None,
        budget=BudgetService(
            store,
            BudgetPolicy(
                allowances_micros={
                    "knowledge_verdict": settings.INTELLIGENCE_KNOWLEDGE_ALLOWANCE_MICROS or 0
                },
                rate_revision=settings.INTELLIGENCE_RATE_REVISION,
            ),
        ),
        reservation_payload={
            "operation_id": f"knowledge-verdict:{job.job_id}",
            "operation_type": "knowledge_verdict",
            "candidate_id": job.candidate_id,
            "job_id": job.job_id,
            "policy_revision": "knowledge-verdict-v1",
            "provider": "insight_oauth",
            "rate_revision": settings.INTELLIGENCE_RATE_REVISION,
            "maximum_micros": settings.INTELLIGENCE_KNOWLEDGE_ALLOWANCE_MICROS or 0,
            "allowance_class": "knowledge_verdict",
        },
    )
    return insight.model_copy(update={"knowledge_verdict": verdict})


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


async def run_oauth_scoped_candidate_worker_tick(
    store: InsightStore, candidate_id: str
) -> InsightRevision | None:
    """Run OAuth analysis for only one explicitly named Candidate."""
    return await process_scoped_candidate_one(store, candidate_id, build_insight_llm_provider())


async def run_oauth_worker_tick(store: InsightStore) -> InsightRevision | None:
    """Execute one bounded OAuth worker tick for an already-initialized store."""
    return await process_one_oauth(store)
