"""One bounded worker tick for fixture-safe personal insight analysis."""

import logging
from pathlib import Path

from app.factory import build_s2k_llm_provider
from app.llm.json_call import MAX_REPAIR_ATTEMPTS
from app.llm.protocol import LLMProvider
from app.llm.s2k_bridge import S2KBridgeProvider
from app.models.insights import EvidenceBundle, InsightJob, InsightRevision
from app.services.context_loader import ContextLoader
from app.services.insight_analysis import analyze_bundle
from app.services.insight_budget import BudgetPolicy, BudgetService, utc_day_window
from app.services.insight_context import load_prepared_context
from app.services.insight_knowledge_verdict import judge_knowledge
from app.services.insight_product_relevance import judge_relevance, load_rubric, not_judged
from app.services.insight_projection import reconcile_store_projections
from app.services.product_relevance_input import build_product_inputs
from app.storage.insight_store import InsightStore
from config import settings

logger = logging.getLogger(__name__)


def _s2k_inference_lease_seconds(llm: S2KBridgeProvider) -> float:
    """Cover both JSON stages and persistence using the enforced parent bound."""
    return max(
        120.0, 2 * (1 + MAX_REPAIR_ATTEMPTS) * llm.completion_timeout_seconds + 60
    )


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
    if isinstance(llm, S2KBridgeProvider):
        llm.bind_job_id(job.job_id)
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
        if isinstance(llm, S2KBridgeProvider):
            store.mark_job_inference_started(
                job.job_id, job.lease_token or "",
                lease_seconds=_s2k_inference_lease_seconds(llm),
            )
        insight = await analyze_bundle(bundle, prepared, llm)
    except Exception as exc:
        store.fail_job_retryable(job.job_id, job.lease_token or "", str(exc))
        raise
    if job.supersedes_insight_id:
        insight = insight.model_copy(update={"supersedes_insight_id": job.supersedes_insight_id})
    insight = await _attach_knowledge_verdict(store, job, insight, llm)
    insight = await _attach_product_relevance(
        store, job, insight, bundle, llm,
        decision_context_root if decision_context_root is not None
        else settings.DECISION_CONTEXT_ROOT,
    )
    completed = store.complete_job_analysis(job.job_id, job.lease_token or "", prepared, insight)
    if settings.INSIGHT_PROJECTION_ENABLED:
        try:
            reconcile_store_projections(
                store, Path(settings.WIKI_ROOT) / "outputs" / "signal-intelligence"
            )
        except Exception:
            # The Insight and job are already committed. A restart can rebuild
            # the derived files; never turn this into a retry of paid analysis.
            logger.exception("Could not update Insight projection after job completion")
    return completed


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
            "maximum_micros": settings.INTELLIGENCE_KNOWLEDGE_MAXIMUM_MICROS or 0,
            "allowance_class": "knowledge_verdict",
            "budget_window": utc_day_window(),
        },
    )
    return insight.model_copy(update={"knowledge_verdict": verdict})


async def _attach_product_relevance(
    store: InsightStore, job: InsightJob, insight: InsightRevision, bundle: EvidenceBundle,
    llm: LLMProvider, decision_context_root: str,
) -> InsightRevision:
    """Attach a non-blocking relevance verdict; off leaves the field null."""
    if not settings.INSIGHT_PRODUCT_RELEVANCE_ENABLED:
        return insight
    rubric = load_rubric(settings.PRODUCT_RELEVANCE_RUBRIC_PATH)
    try:
        products = (
            build_product_inputs(ContextLoader(decision_context_root), rubric.eligible)
            if rubric is not None else []
        )
    except Exception:
        return insight.model_copy(update={"product_relevance": not_judged(
            "product context could not be read",
            rubric_revision=rubric.revision if rubric else None,
        )})
    relevance = await judge_relevance(
        insight=insight, bundle=bundle, products=products, rubric=rubric, llm=llm,
        budget=BudgetService(
            store,
            BudgetPolicy(
                allowances_micros={
                    "product_relevance":
                        settings.INTELLIGENCE_PRODUCT_RELEVANCE_ALLOWANCE_MICROS or 0
                },
                rate_revision=settings.INTELLIGENCE_RATE_REVISION,
            ),
        ),
        reservation_payload={
            "operation_id": f"product-relevance:{job.job_id}",
            "operation_type": "product_relevance",
            "candidate_id": job.candidate_id,
            "job_id": job.job_id,
            "policy_revision": "product-relevance-v1",
            "provider": "insight_oauth",
            "rate_revision": settings.INTELLIGENCE_RATE_REVISION,
            "maximum_micros": settings.INTELLIGENCE_PRODUCT_RELEVANCE_MAXIMUM_MICROS or 0,
            "allowance_class": "product_relevance",
            "budget_window": utc_day_window(),
        },
    )
    return insight.model_copy(update={"product_relevance": relevance})


async def process_one_oauth(store: InsightStore) -> InsightRevision | None:
    """Claim at most one learning job with the isolated S2K completion bridge."""
    return await process_one(store, build_s2k_llm_provider())


async def run_oauth_backfill_worker_tick(
    store: InsightStore, backfill_id: str
) -> InsightRevision | None:
    """Run one S2K analysis step for exactly one explicitly named backfill."""
    job = store.claim_backfill_job(backfill_id)
    if job is None:
        return None
    return await _process_claimed_job(store, job, build_s2k_llm_provider())


async def run_oauth_scoped_candidate_worker_tick(
    store: InsightStore, candidate_id: str
) -> InsightRevision | None:
    """Run S2K analysis for only one explicitly named Candidate."""
    return await process_scoped_candidate_one(store, candidate_id, build_s2k_llm_provider())


async def run_oauth_worker_tick(store: InsightStore) -> InsightRevision | None:
    """Execute one bounded OAuth worker tick for an already-initialized store."""
    return await process_one_oauth(store)
