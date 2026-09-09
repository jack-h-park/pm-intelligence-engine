"""Authenticated fixture-intake routes for immutable insight records."""

import hashlib
import json
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import get_engine
from app.factory import PMEngine
from app.models.insights import (
    Candidate,
    EvidenceBundle,
    EvidenceDate,
    Passage,
    SourceExcerpt,
    SourceRecord,
)
from app.storage.insight_store import (
    IdempotencyConflict,
    InvalidInsightReference,
    MissingInsightRecord,
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


def _key(idempotency_key: str | None) -> str:
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Idempotency-Key is required",
        )
    return idempotency_key


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
