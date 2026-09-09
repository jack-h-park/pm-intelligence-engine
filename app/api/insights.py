"""Authenticated fixture-intake routes for immutable insight records."""

import hashlib
import json
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import get_engine
from app.factory import PMEngine
from app.models.decision_case import InsightRevisionReference
from app.models.insights import (
    Candidate,
    EvidenceBundle,
    EvidenceDate,
    InsightJob,
    Passage,
    ResearchRequest,
    SourceExcerpt,
    SourceRecord,
)
from app.services.decision_case import build_decision_case
from app.storage.insight_store import (
    IdempotencyConflict,
    InvalidInsightReference,
    MissingInsightRecord,
    StaleLease,
)

router = APIRouter(tags=["insights"])


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CandidateCreate(_Request):
    origin: Literal["discovered", "user_supplied", "legacy_import"]
    subject: str = Field(min_length=1)
    question_ids: list[str]
    source_ids: list[str] = Field(default_factory=list)
    policy_revision: str = Field(min_length=1)


class SourceCreate(_Request):
    candidate_id: str
    origin: Literal["discovered", "user_supplied", "legacy_import"]
    content_hash: str = Field(min_length=64, max_length=64)
    acquisition_status: Literal["ok", "fallback_summary", "low_quality", "fetch_failed"]
    retrieved_at: str
    content: str | None = None
    excerpts: list[SourceExcerpt] = Field(default_factory=list)
    url: str | None = None
    legacy_reference: str | None = None


class BundleCreate(_Request):
    candidate_id: str
    source_ids: list[str] = Field(min_length=1, max_length=4)
    passages: list[Passage]
    dates: list[EvidenceDate] = Field(default_factory=list)
    coverage_gaps: list[str] = Field(default_factory=list)
    freshness_status: Literal["current", "background", "superseded", "unknown"]
    context_revision: str = Field(min_length=1)


class JobCreate(_Request):
    candidate_id: str
    context_revision: str = Field(min_length=1)
    purpose: Literal["learning", "decision_preparation"]
    bundle_id: str | None = None


class JobAccepted(BaseModel):
    job_id: str
    state: Literal["queued"]


class DecisionRequestCreate(_Request):
    prepared_context_id: str = Field(min_length=1)
    prepared_context_revision: int = Field(ge=1)
    insight_references: list[InsightRevisionReference] = Field(default_factory=list)
    product_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    depth: Literal["archive", "note", "structure", "evaluate", "decide"] | None = None
    options: list[str] = Field(default_factory=list)
    decision_pipeline_version: Literal["legacy", "evidence_v1"] = "legacy"


class DecisionRequestAccepted(BaseModel):
    request_id: str
    run_id: str


class ResearchClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter_id: str = Field(min_length=1)


class ResearchResults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_token: str = Field(min_length=1)
    results: list[dict]
    failures: list[dict] = Field(default_factory=list)


def _request_hash(body: BaseModel) -> str:
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _actor_fingerprint(authorization: str | None) -> str:
    # Authentication validates the bearer token globally.  Persist only a hash
    # so idempotency is scoped to the actor without retaining a credential.
    return hashlib.sha256((authorization or "").encode("utf-8")).hexdigest()


def _store(engine: PMEngine):
    from config import settings

    if not settings.INSIGHT_WRITES_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Insight writes are disabled",
        )
    if engine.insight_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Insight storage is unavailable",
        )
    return engine.insight_store


def _processing_store(engine: PMEngine):
    from config import settings

    store = _store(engine)
    if settings.INTELLIGENCE_MODE not in {"shadow", "insights"}:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Insight processing is disabled",
        )
    return store


def _key(idempotency_key: str | None) -> str:
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Idempotency-Key is required",
        )
    return idempotency_key


@router.post(
    "/decision-requests",
    response_model=DecisionRequestAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_decision_request(
    body: DecisionRequestCreate,
    background_tasks: BackgroundTasks,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authorization: str | None = Header(default=None),
    engine: PMEngine = Depends(get_engine),
) -> DecisionRequestAccepted:
    """Bridge prepared evidence into exactly one legacy-compatible decision run."""
    from app.api.runs import _execute_s1_s2, _validate_product_exists, validate_mode_for_product
    from config import settings

    if body.decision_pipeline_version == "evidence_v1" and not settings.DECISION_PIPELINE_V2_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="evidence_v1 decision pipeline is disabled",
        )

    insight_store = _store(engine)
    prepared = insight_store.get_prepared_context(body.prepared_context_id)
    if prepared is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Prepared context not found"
        )
    if prepared.revision != body.prepared_context_revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Prepared context revision changed"
        )
    for reference in body.insight_references:
        insight = insight_store.get_insight(reference.insight_id)
        if insight is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Insight not found")
        if insight.revision != reference.revision:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Insight revision changed"
            )
    _validate_product_exists(body.product_id, engine)
    if body.depth is not None:
        validate_mode_for_product(body.depth, body.product_id)
    case = build_decision_case(
        prepared_context=prepared,
        product_id=body.product_id,
        decision_question=body.question,
        authorized_depth=body.depth,
        input_origin="insight" if body.insight_references else "direct",
        insight_references=body.insight_references,
        options=body.options,
    )
    try:
        result, stored_status = engine.store.create_idempotent_decision_request(
            _actor_fingerprint(authorization),
            _key(idempotency_key),
            _request_hash(body),
            case,
            body.decision_pipeline_version,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if stored_status == status.HTTP_202_ACCEPTED:
        background_tasks.add_task(
            _execute_s1_s2,
            result["run_id"],
            result["signal_id"],
            body.product_id,
            body.depth,
            engine,
        )
    response.status_code = stored_status
    return DecisionRequestAccepted(request_id=result["request_id"], run_id=result["run_id"])


@router.post("/insight-candidates", response_model=Candidate, status_code=status.HTTP_201_CREATED)
async def create_candidate(
    body: CandidateCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authorization: str | None = Header(default=None),
    engine: PMEngine = Depends(get_engine),
) -> Candidate:
    try:
        candidate, status_code = _store(engine).save_idempotent_candidate(
            _actor_fingerprint(authorization),
            _key(idempotency_key),
            _request_hash(body),
            body.model_dump(),
        )
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    response.status_code = status_code
    return candidate


@router.post("/insight-sources", response_model=SourceRecord, status_code=status.HTTP_201_CREATED)
async def create_source(
    body: SourceCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authorization: str | None = Header(default=None),
    engine: PMEngine = Depends(get_engine),
) -> SourceRecord:
    try:
        source, status_code = _store(engine).save_idempotent_source(
            _actor_fingerprint(authorization),
            _key(idempotency_key),
            _request_hash(body),
            body.model_dump(),
        )
    except MissingInsightRecord as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    response.status_code = status_code
    return source


@router.post(
    "/insight-evidence", response_model=EvidenceBundle, status_code=status.HTTP_201_CREATED
)
async def create_bundle(
    body: BundleCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authorization: str | None = Header(default=None),
    engine: PMEngine = Depends(get_engine),
) -> EvidenceBundle:
    try:
        bundle, status_code = _store(engine).save_idempotent_bundle(
            _actor_fingerprint(authorization),
            _key(idempotency_key),
            _request_hash(body),
            body.model_dump(),
        )
    except (InvalidInsightReference, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    response.status_code = status_code
    return bundle


@router.post("/insight-jobs", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_job(
    body: JobCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authorization: str | None = Header(default=None),
    engine: PMEngine = Depends(get_engine),
) -> JobAccepted:
    try:
        job, stored_status = _processing_store(engine).create_idempotent_job(
            _actor_fingerprint(authorization),
            _key(idempotency_key),
            _request_hash(body),
            body.model_dump(),
        )
    except MissingInsightRecord as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InvalidInsightReference as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    response.status_code = status.HTTP_202_ACCEPTED if stored_status == 201 else status.HTTP_200_OK
    return JobAccepted(job_id=job.job_id, state="queued")


@router.get("/insight-jobs/{job_id}", response_model=InsightJob)
async def get_job(job_id: str, engine: PMEngine = Depends(get_engine)) -> InsightJob:
    if engine.insight_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Insight storage is unavailable"
        )
    job = engine.insight_store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job


@router.post("/insight-research/claim", response_model=ResearchRequest)
async def claim_research(
    body: ResearchClaim,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    engine: PMEngine = Depends(get_engine),
) -> ResearchRequest | None:
    _key(idempotency_key)
    request = _processing_store(engine).claim_research(body.adapter_id)
    if request is None:
        response.status_code = status.HTTP_204_NO_CONTENT
    return request


@router.post("/insight-research/{request_id}/results", response_model=ResearchRequest)
async def submit_research_results(
    request_id: str,
    body: ResearchResults,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    engine: PMEngine = Depends(get_engine),
) -> ResearchRequest:
    _key(idempotency_key)
    try:
        return _processing_store(engine).submit_research_results(
            request_id, body.lease_token, body.results, body.failures
        )
    except MissingInsightRecord as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (StaleLease, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
