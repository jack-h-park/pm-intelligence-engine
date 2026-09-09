"""One bounded worker tick for fixture-safe personal insight analysis."""

from app.llm.protocol import LLMProvider
from app.models.insights import InsightRevision, PreparedContext
from app.services.insight_analysis import analyze_bundle
from app.storage.insight_store import InsightStore


async def process_one(store: InsightStore, llm: LLMProvider) -> InsightRevision | None:
    """Claim one job and atomically save its prepared context, insight, and completion.

    Acquisition requests are handled by the control-plane adapter. This worker
    only processes a job after a bundle has been persisted by the engine.
    """
    job = store.claim_job()
    if job is None:
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
    status = "valid" if bundle.passages else "needs_evidence"
    prepared = PreparedContext(
        candidate_id=candidate.candidate_id,
        bundle_id=bundle.bundle_id,
        question=candidate.question_ids[0] if candidate.question_ids else candidate.subject,
        facts=[],
        unresolved_questions=bundle.coverage_gaps,
        validation_status=status,
        context_revision=job.context_revision,
    )
    insight = await analyze_bundle(bundle, prepared, llm)
    return store.complete_job_analysis(job.job_id, job.lease_token or "", prepared, insight)
