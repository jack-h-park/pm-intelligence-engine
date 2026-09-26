"""Authenticated fixture-intake routes for immutable insight records."""

import hashlib
import json
import secrets
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Query,
    Response,
    status,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.deps import get_engine
from app.factory import PMEngine, build_s2k_llm_provider
from app.insight_worker import (
    run_oauth_backfill_worker_tick,
    run_oauth_scoped_candidate_worker_tick,
)
from app.llm.protocol import LLMProvider
from app.models.decision_case import InsightRevisionReference
from app.models.insights import (
    Candidate,
    EvidenceBackfillRequest,
    EvidenceBackfillTarget,
    EvidenceBundle,
    EvidenceDate,
    InsightClaimPassageLink,
    InsightEvidencePassage,
    InsightEvidenceSource,
    InsightEvidenceView,
    InsightJob,
    InsightReview,
    InsightRevision,
    Passage,
    ResearchRequest,
    SourceExcerpt,
    SourceRecord,
)
from app.services.decision_case import build_decision_case
from app.services.insight_budget import BudgetPolicy, BudgetService, utc_day_window
from app.services.insight_context import resolve_interest
from app.services.insight_delivery import confirm_delivery, queue_delivery
from app.services.insight_migration import build_dry_run_inventory
from app.services.insight_search import search_insights
from app.services.insight_sync import InsightListCursor, decode_cursor, encode_cursor
from app.services.insight_triage import TriageBudgetDenied, TriageDecision, triage_with_reservation
from app.services.product_connections import ProductConnectionAssessment, ProductConnectionService
from app.storage.insight_store import (
    IdempotencyConflict,
    InsightStore,
    InvalidInsightReference,
    MissingInsightRecord,
    StaleLease,
    TriageOperationNotRunning,
)

router = APIRouter(tags=["insights"])


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InsightOperationReservationStatus(BaseModel):
    reservation_id: str
    state: Literal["reserved", "finalized", "unknown"]
    allowance_class: str
    maximum_micros: int
    actual_micros: int | None


class InsightOperationStatus(BaseModel):
    operation_id: str
    triage_state: str | None
    has_triage_result: bool
    reservation: InsightOperationReservationStatus | None


class ReconcileUnknownOperationRequest(_Request):
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def reason_must_have_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must contain non-whitespace text")
        return value.strip()


class ReconciledOperation(BaseModel):
    operation_id: str
    triage_state: Literal["terminal_unknown"]
    operator_id: str
    reason: str
    created_at: datetime


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


class ScopedCandidateWorkerStatus(BaseModel):
    candidate_id: str
    job_id: str
    state: str
    completion_disposition: str | None
    insight_id: str | None = None


class EvidenceBackfillCreate(_Request):
    base_revision: int = Field(ge=1)
    targets: list[EvidenceBackfillTarget] = Field(min_length=1, max_length=3)


class DecisionRequestCreate(_Request):
    prepared_context_id: str = Field(min_length=1)
    prepared_context_revision: int = Field(ge=1)
    insight_references: list[InsightRevisionReference] = Field(default_factory=list)
    product_id: str = Field(min_length=1)
    confirmed_product_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    depth: Literal["archive", "note", "structure", "evaluate", "decide"] | None = None
    options: list[str] = Field(default_factory=list)
    decision_pipeline_version: Literal["legacy", "evidence_v1"] = "legacy"


class InsightDecisionRequestCreate(_Request):
    """Human-confirmed decision choices for one immutable learning Insight."""

    revision: int = Field(ge=1)
    product_id: str = Field(min_length=1)
    confirmed_product_id: str = Field(min_length=1)
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
    results: list[dict[str, Any]]
    failures: list[dict[str, Any]] = Field(default_factory=list)


class InsightSearchResults(BaseModel):
    items: list[InsightRevision]
    next_cursor: str | None = None


class NoveltyLookup(_Request):
    content_hashes: list[str] = Field(min_length=1, max_length=20)

    @field_validator("content_hashes")
    @classmethod
    def _validate_hashes(cls, values: list[str]) -> list[str]:
        for value in values:
            if len(value) != 64:
                raise ValueError("content hashes must be SHA-256 hex strings")
            try:
                int(value, 16)
            except ValueError as exc:
                raise ValueError("content hashes must be SHA-256 hex strings") from exc
        return values


class NoveltyLookupResult(BaseModel):
    known_content_hashes: list[str]


class MigrationManifestCreate(_Request):
    original_system: str = Field(min_length=1)
    original_id: str = Field(min_length=1)
    snapshot_hash: str = Field(min_length=64, max_length=64)
    classification: str = Field(min_length=1)
    migration_state: Literal["unreviewed"] = "unreviewed"
    notification_handling: Literal["none"] = "none"


class MigrationManifestResults(BaseModel):
    items: list[MigrationManifestCreate]


class MigrationInventoryAccepted(BaseModel):
    inventory_id: str
    inventory_hash: str
    high_water_candidate_id: str | None
    records: list[dict[str, Any]]


class MigrationImportRequest(_Request):
    inventory_hash: str = Field(min_length=64, max_length=64)
    batch_size: int = Field(default=100, ge=1, le=100)


class MigrationImportAccepted(BaseModel):
    inventory_id: str
    imported_count: int
    complete: bool


class MigrationOverlayRequest(_Request):
    inventory_hash: str = Field(min_length=64, max_length=64)
    enabled: bool


class MigrationOverlayAccepted(BaseModel):
    inventory_id: str
    enabled: bool


class SemanticTriageRequest(_Request):
    question: str = Field(min_length=1)
    title: str = Field(min_length=1)
    content: str = Field(min_length=1, max_length=2 * 1024 * 1024)
    operation_id: str = Field(min_length=1)
    policy_revision: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    rate_revision: str = Field(min_length=1)
    maximum_micros: int = Field(gt=0)
    actual_micros: int | Literal["unknown"] = "unknown"


class InterestTriageRequest(_Request):
    interest_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    content: str = Field(min_length=1, max_length=2 * 1024 * 1024)
    operation_id: str = Field(min_length=1)
    policy_revision: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    rate_revision: str = Field(min_length=1)
    maximum_micros: int = Field(gt=0)
    actual_micros: int | Literal["unknown"] = "unknown"


def _triage_budget(engine: PMEngine) -> BudgetService:
    from config import settings

    store = _processing_store(engine)
    return BudgetService(
        store,
        BudgetPolicy(
            allowances_micros={"sensing": settings.INTELLIGENCE_SENSING_ALLOWANCE_MICROS or 0},
            rate_revision=settings.INTELLIGENCE_RATE_REVISION,
        ),
    )


class DeliveryReceiptCreate(_Request):
    revision: int = Field(ge=1)
    channel: Literal["telegram", "discord"]
    state: Literal["queued", "sent", "unknown"]


class DeliveryReceiptAccepted(BaseModel):
    receipt_id: str
    insight_id: str
    revision: int
    channel: str
    state: str


class InsightFeedbackCreate(_Request):
    revision: int = Field(ge=1)
    label: Literal["useful", "already_known", "wrong", "weak_connection", "too_shallow"]


class InsightFeedbackAccepted(BaseModel):
    feedback_id: str
    insight_id: str
    revision: int
    label: str


class InsightReviewCreate(_Request):
    revision: int = Field(ge=1)
    disposition: Literal["retain", "needs_evidence", "not_useful"]
    note: str | None = Field(default=None, max_length=2000)


class InsightReviewList(BaseModel):
    items: list[InsightReview]


def _request_hash(body: BaseModel) -> str:
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _actor_fingerprint(authorization: str | None) -> str:
    # Authentication validates the bearer token globally.  Persist only a hash
    # so idempotency is scoped to the actor without retaining a credential.
    return hashlib.sha256((authorization or "").encode("utf-8")).hexdigest()


def _store(engine: PMEngine) -> InsightStore:
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


def _read_store(engine: PMEngine) -> InsightStore:
    if engine.insight_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Insight storage is unavailable",
        )
    return engine.insight_store


def _processing_store(engine: PMEngine) -> InsightStore:
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

    if (
        body.decision_pipeline_version == "evidence_v1"
        and not settings.DECISION_PIPELINE_V2_ENABLED
    ):
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
    if prepared.validation_status != "valid":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Prepared context needs evidence before a decision request",
        )
    if body.confirmed_product_id != body.product_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Confirmed product must match product_id",
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
        confirmed_product_id=body.confirmed_product_id,
        confirmed_by_actor=_actor_fingerprint(authorization),
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


@router.post(
    "/insights/{insight_id}/decision-requests",
    response_model=DecisionRequestAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_insight_decision_request(
    insight_id: str,
    body: InsightDecisionRequestCreate,
    background_tasks: BackgroundTasks,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authorization: str | None = Header(default=None),
    engine: PMEngine = Depends(get_engine),
) -> DecisionRequestAccepted:
    """Create a decision request from a reviewer-selected Insight revision."""
    from config import settings

    if not settings.DECISION_PIPELINE_V2_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Decision pipeline is disabled",
        )
    insight_store = _store(engine)
    insight = insight_store.get_insight(insight_id)
    if insight is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Insight not found")
    if insight.revision != body.revision:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Insight revision changed")
    prepared = insight_store.get_prepared_context(insight.prepared_context_id)
    if prepared is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Prepared context not found"
        )
    return await create_decision_request(
        DecisionRequestCreate(
            prepared_context_id=prepared.prepared_context_id,
            prepared_context_revision=prepared.revision,
            insight_references=[
                InsightRevisionReference(insight_id=insight.insight_id, revision=insight.revision)
            ],
            product_id=body.product_id,
            confirmed_product_id=body.confirmed_product_id,
            question=body.question,
            depth=body.depth,
            options=body.options,
            decision_pipeline_version=body.decision_pipeline_version,
        ),
        background_tasks,
        response,
        idempotency_key,
        authorization,
        engine,
    )


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


@router.post("/insight-migrations", status_code=status.HTTP_201_CREATED)
async def create_migration_manifest(
    body: MigrationManifestCreate,
    engine: PMEngine = Depends(get_engine),
) -> dict[str, Any]:
    """Store a review-only legacy overlay; it cannot start processing work."""
    try:
        return _store(engine).save_migration_manifest(body.model_dump())
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/insight-migrations", response_model=MigrationManifestResults)
async def list_migration_manifests(
    classification: str | None = Query(default=None, min_length=1),
    limit: int = Query(default=100, ge=1, le=500),
    engine: PMEngine = Depends(get_engine),
) -> MigrationManifestResults:
    """List read-only migration overlays without reprocessing legacy records."""
    manifests = _read_store(engine).list_migration_manifests()
    if classification is not None:
        manifests = [item for item in manifests if item["classification"] == classification]
    return MigrationManifestResults(
        items=[MigrationManifestCreate.model_validate(item) for item in manifests[:limit]]
    )


@router.post(
    "/insight-migration-inventories",
    response_model=MigrationInventoryAccepted,
    status_code=status.HTTP_201_CREATED,
)
async def create_migration_inventory(
    engine: PMEngine = Depends(get_engine),
) -> MigrationInventoryAccepted:
    """Create a durable, read-only migration plan; no import or delivery work occurs."""
    store = _store(engine)
    inventory = build_dry_run_inventory(store)
    inventory_id = str(uuid.uuid4())
    payload: dict[str, Any] = {
        "inventory_id": inventory_id,
        "inventory_hash": inventory.inventory_hash,
        "high_water_candidate_id": inventory.high_water_candidate_id,
        "records": inventory.records,
    }
    stored = store.save_migration_inventory(inventory_id, inventory.inventory_hash, payload)
    return MigrationInventoryAccepted(**stored)


@router.get(
    "/insight-migration-inventories/{inventory_id}",
    response_model=MigrationInventoryAccepted,
)
async def get_migration_inventory(
    inventory_id: str, engine: PMEngine = Depends(get_engine)
) -> MigrationInventoryAccepted:
    inventory = _read_store(engine).get_migration_inventory(inventory_id)
    if inventory is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Migration inventory not found"
        )
    return MigrationInventoryAccepted(**inventory)


@router.post(
    "/insight-migration-inventories/{inventory_id}/import",
    response_model=MigrationImportAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def import_migration_inventory(
    inventory_id: str,
    body: MigrationImportRequest,
    engine: PMEngine = Depends(get_engine),
) -> MigrationImportAccepted:
    """Import only reconciled migration metadata; no source or delivery data changes."""
    try:
        result = _store(engine).import_migration_inventory(
            inventory_id, body.inventory_hash, batch_size=body.batch_size
        )
    except MissingInsightRecord as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return MigrationImportAccepted(
        inventory_id=inventory_id,
        imported_count=int(result["imported_count"]),
        complete=bool(result["complete"]),
    )


@router.post(
    "/insight-migration-inventories/{inventory_id}/overlay",
    response_model=MigrationOverlayAccepted,
)
async def set_migration_overlay(
    inventory_id: str,
    body: MigrationOverlayRequest,
    engine: PMEngine = Depends(get_engine),
) -> MigrationOverlayAccepted:
    """Activate an imported read overlay only when the operator release flag permits it."""
    from config import settings

    if body.enabled and not settings.INSIGHT_MIGRATION_ACTIVATION_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Migration overlay activation is disabled by the release flag",
        )
    try:
        result = _store(engine).set_migration_overlay(
            inventory_id, body.inventory_hash, enabled=body.enabled
        )
    except MissingInsightRecord as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return MigrationOverlayAccepted(inventory_id=inventory_id, **result)


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
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
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


@router.post(
    "/insights/{insight_id}/evidence-backfills",
    response_model=EvidenceBackfillRequest,
    status_code=status.HTTP_201_CREATED,
)
async def create_evidence_backfill(
    insight_id: str,
    body: EvidenceBackfillCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authorization: str | None = Header(default=None),
    engine: PMEngine = Depends(get_engine),
) -> EvidenceBackfillRequest:
    """Start a review-only evidence request without selecting a product or decision path."""
    try:
        request, stored_status = _processing_store(engine).create_idempotent_backfill(
            _actor_fingerprint(authorization),
            _key(idempotency_key),
            _request_hash(body),
            insight_id=insight_id,
            base_revision=body.base_revision,
            targets=body.targets,
        )
    except MissingInsightRecord as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InvalidInsightReference as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    response.status_code = stored_status
    return request


@router.post(
    "/evidence-backfills/{backfill_id}/worker-tick",
    response_model=EvidenceBackfillRequest,
)
async def tick_evidence_backfill_worker(
    backfill_id: str,
    authorization: str | None = Header(default=None),
    engine: PMEngine = Depends(get_engine),
) -> EvidenceBackfillRequest:
    """Advance one named backfill without ever falling through to the generic queue."""
    store = _processing_store(engine)
    if store.get_backfill(backfill_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="backfill was not found")
    try:
        await run_oauth_backfill_worker_tick(store, backfill_id)
    except (InvalidInsightReference, MissingInsightRecord) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    request = store.get_backfill(backfill_id)
    if request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="backfill was not found")
    return request


@router.post(
    "/insight-candidates/{candidate_id}/worker-tick",
    response_model=ScopedCandidateWorkerStatus,
)
async def tick_scoped_candidate_worker(
    candidate_id: str,
    authorization: str | None = Header(default=None),
    engine: PMEngine = Depends(get_engine),
) -> ScopedCandidateWorkerStatus:
    """Advance only the named Candidate using its persisted evidence."""
    store = _processing_store(engine)
    if store.get_candidate(candidate_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="candidate was not found")
    try:
        insight = await run_oauth_scoped_candidate_worker_tick(store, candidate_id)
        job = store.ensure_scoped_candidate_job(candidate_id)
    except (InvalidInsightReference, MissingInsightRecord) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    if insight is None and job.prepared_context_id is not None:
        insight = store.get_insight_for_prepared_context(job.prepared_context_id)
    return ScopedCandidateWorkerStatus(
        candidate_id=candidate_id,
        job_id=job.job_id,
        state=job.state,
        completion_disposition=job.completion_disposition,
        insight_id=insight.insight_id if insight else None,
    )


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


@router.get("/insights/search", response_model=InsightSearchResults)
async def search(
    q: str = Query(min_length=1),
    limit: int = Query(default=20, ge=1, le=100),
    engine: PMEngine = Depends(get_engine),
) -> InsightSearchResults:
    if engine.insight_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Insight storage is unavailable",
        )
    return InsightSearchResults(items=search_insights(engine.insight_store, q)[:limit])


@router.get("/insights", response_model=InsightSearchResults)
async def list_insights(
    since: datetime | None = Query(default=None),
    after: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    engine: PMEngine = Depends(get_engine),
) -> InsightSearchResults:
    """List Engine-owned learning insights without joining product workflow state."""
    if engine.insight_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Insight storage is unavailable"
        )
    cursor = None
    if after is not None:
        try:
            cursor = decode_cursor(after)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid insight cursor") from exc
        requested_since = since.astimezone(UTC) if since is not None else None
        if since is not None and cursor.since != requested_since:
            raise HTTPException(
                status_code=422, detail="cursor since boundary does not match request"
            )
    normalized_since = (
        since.astimezone(UTC)
        if since is not None
        else cursor.since if cursor is not None else None
    )
    items, _has_more = engine.insight_store.list_insights_since(
        normalized_since,
        (cursor.created_at, cursor.insight_id) if cursor is not None else None,
        limit,
    )
    next_cursor = None
    if items:
        final = items[-1]
        next_cursor = encode_cursor(
            InsightListCursor(
                since=normalized_since,
                created_at=final.created_at,
                insight_id=final.insight_id,
            )
        )
    return InsightSearchResults(items=items, next_cursor=next_cursor)


@router.post("/insight-triage/novelty", response_model=NoveltyLookupResult)
async def novelty_lookup(
    body: NoveltyLookup, engine: PMEngine = Depends(get_engine)
) -> NoveltyLookupResult:
    if engine.insight_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Insight storage is unavailable",
        )
    return NoveltyLookupResult(
        known_content_hashes=engine.insight_store.known_source_hashes(body.content_hashes)
    )


async def _semantic_triage(
    body: SemanticTriageRequest | InterestTriageRequest,
    engine: PMEngine,
    *,
    question: str,
    llm: LLMProvider | None = None,
    llm_factory: Callable[[str], LLMProvider] | None = None,
) -> TriageDecision:
    store = _processing_store(engine)
    claim_state, cached = store.claim_triage(body.operation_id)
    if claim_state == "complete" and cached is not None:
        return TriageDecision.model_validate(cached)
    if claim_state != "claimed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="triage_in_progress")
    if llm_factory is not None:
        try:
            llm = llm_factory(body.operation_id)
        except Exception:
            store.abandon_triage_claim(body.operation_id)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="S2K inference is unavailable",
            ) from None
    try:
        decision = await triage_with_reservation(
            question=question,
            title=body.title,
            content=body.content,
            llm=llm if llm is not None else engine.llm,
            budget=_triage_budget(engine),
            reservation_payload={
                "operation_id": body.operation_id,
                "operation_type": "semantic_triage",
                "policy_revision": body.policy_revision,
                "provider": body.provider,
                "rate_revision": body.rate_revision,
                "maximum_micros": body.maximum_micros,
                "allowance_class": "sensing",
                "budget_window": utc_day_window(),
            },
            actual_micros=body.actual_micros,
        )
    except TriageBudgetDenied as exc:
        store.abandon_triage_claim(body.operation_id)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="budget_denied") from exc
    try:
        store.complete_triage(body.operation_id, decision.model_dump(mode="json"))
    except TriageOperationNotRunning as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="triage_in_progress"
        ) from exc
    return decision


@router.post("/insight-triage", response_model=TriageDecision)
async def semantic_triage(
    body: SemanticTriageRequest, engine: PMEngine = Depends(get_engine)
) -> TriageDecision:
    return await _semantic_triage(body, engine, question=body.question)


@router.post("/insight-triage/interest", response_model=TriageDecision)
async def interest_semantic_triage(
    body: InterestTriageRequest, engine: PMEngine = Depends(get_engine)
) -> TriageDecision:
    from config import settings

    resolved = resolve_interest([body.interest_id], settings.DECISION_CONTEXT_ROOT)
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="unknown_interest_id",
        )
    return await _semantic_triage(
        body,
        engine,
        question=resolved.question,
        llm_factory=build_s2k_llm_provider,
    )


@router.get("/insight-operations")
async def insight_operations(engine: PMEngine = Depends(get_engine)) -> dict[str, Any]:
    """Read-only shadow operations counters; never enables intake or delivery."""
    if engine.insight_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Insight storage is unavailable",
        )
    return engine.insight_store.operational_summary()


@router.get(
    "/insight-operations/{operation_id}", response_model=InsightOperationStatus
)
async def insight_operation_status(
    operation_id: str, engine: PMEngine = Depends(get_engine)
) -> InsightOperationStatus:
    """Read sanitized status for one triage operation without exposing its payload."""
    if engine.insight_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Insight storage is unavailable",
        )
    operation = engine.insight_store.get_operation_status(operation_id)
    if operation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Operation not found")
    return InsightOperationStatus.model_validate(operation)


@router.post(
    "/insight-operations/{operation_id}/reconcile-unknown",
    response_model=ReconciledOperation,
)
async def reconcile_unknown_operation(
    operation_id: str,
    body: ReconcileUnknownOperationRequest,
    reconciliation_token: str | None = Header(
        default=None, alias="X-S2K-Reconciliation-Token"
    ),
    engine: PMEngine = Depends(get_engine),
) -> ReconciledOperation:
    """Fence an ambiguous provider call without releasing its budget reservation."""
    from config import settings

    expected = settings.S2K_RECONCILIATION_TOKEN
    operator_id = settings.S2K_RECONCILIATION_OPERATOR_ID
    if not expected or not operator_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="S2K reconciliation is not configured",
        )
    if not reconciliation_token or not secrets.compare_digest(reconciliation_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid reconciliation credential",
        )
    if engine.insight_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Insight storage is unavailable",
        )
    try:
        result = engine.insight_store.reconcile_unknown_triage(
            operation_id, operator_id=operator_id, reason=body.reason
        )
    except MissingInsightRecord as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Operation not found"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return ReconciledOperation.model_validate(result)


@router.get(
    "/insights/{insight_id}/product-connections", response_model=ProductConnectionAssessment
)
async def get_product_connections(
    insight_id: str,
    revision: int | None = Query(default=None, ge=1),
    engine: PMEngine = Depends(get_engine),
) -> ProductConnectionAssessment:
    """Return ephemeral evidence-grounded candidates without creating workflow work."""
    if engine.insight_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Insight storage is unavailable"
        )
    insight = engine.insight_store.get_insight(insight_id)
    if insight is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Insight not found")
    if revision is not None and revision != insight.revision:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Insight revision changed")
    return ProductConnectionService(engine.context_loader).assess(insight, engine.insight_store)

@router.get("/insights/{insight_id}/evidence", response_model=InsightEvidenceView)
async def get_insight_evidence(
    insight_id: str,
    revision: int = Query(ge=1),
    engine: PMEngine = Depends(get_engine),
) -> InsightEvidenceView:
    if engine.insight_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Insight storage is unavailable",
        )
    evidence = engine.insight_store.get_insight_evidence(insight_id)
    if evidence is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Insight not found")
    insight, prepared_context, bundle, sources = evidence
    if insight.revision != revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Insight revision is stale"
        )

    cited_passage_ids = {
        passage_id for claim in insight.claims for passage_id in claim.passage_ids
    }
    passages = [
        passage for passage in bundle.passages if passage.passage_id in cited_passage_ids
    ]
    cited_source_ids = {passage.source_id for passage in passages}
    cited_sources = [source for source in sources if source.source_id in cited_source_ids]
    source_by_id = {source.source_id: source for source in cited_sources}
    coarse_evidence = any(
        source is not None
        and source.content is not None
        and passage.text.strip() == source.content.strip()
        for passage in passages
        for source in [source_by_id.get(passage.source_id)]
    )

    return InsightEvidenceView(
        insight_id=insight.insight_id,
        revision=insight.revision,
        question=prepared_context.question,
        constraints=prepared_context.constraints,
        sources=[
            InsightEvidenceSource(
                source_id=source.source_id,
                origin=source.origin,
                acquisition_status=source.acquisition_status,
                retrieved_at=source.retrieved_at,
                url=source.url,
                legacy_reference=source.legacy_reference,
            )
            for source in cited_sources
        ],
        passages=[
            InsightEvidencePassage(
                passage_id=passage.passage_id,
                source_id=passage.source_id,
                locator=passage.locator,
                text=passage.text,
                role=passage.role,
            )
            for passage in passages
        ],
        claim_passage_links=[
            InsightClaimPassageLink(claim_index=index, passage_ids=claim.passage_ids)
            for index, claim in enumerate(insight.claims)
        ],
        coarse_evidence=coarse_evidence,
    )


@router.get("/insights/{insight_id}", response_model=InsightRevision)
async def get_insight(insight_id: str, engine: PMEngine = Depends(get_engine)) -> InsightRevision:
    if engine.insight_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Insight storage is unavailable",
        )
    insight = engine.insight_store.get_insight(insight_id)
    if insight is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Insight not found")
    return insight


@router.get("/insights/{insight_id}/reviews", response_model=InsightReviewList)
async def list_insight_reviews(
    insight_id: str, engine: PMEngine = Depends(get_engine)
) -> InsightReviewList:
    if engine.insight_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Insight storage is unavailable",
        )
    if engine.insight_store.get_insight(insight_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Insight not found")
    return InsightReviewList(items=engine.insight_store.list_reviews(insight_id))


@router.post(
    "/insights/{insight_id}/reviews",
    response_model=InsightReview,
    status_code=status.HTTP_201_CREATED,
)
async def create_insight_review(
    insight_id: str,
    body: InsightReviewCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authorization: str | None = Header(default=None),
    engine: PMEngine = Depends(get_engine),
) -> InsightReview:
    """Record a human learning review without promoting the Insight to a decision."""
    try:
        review, stored_status = _store(engine).save_idempotent_review(
            _actor_fingerprint(authorization),
            _key(idempotency_key),
            _request_hash(body),
            {"insight_id": insight_id, **body.model_dump()},
        )
    except MissingInsightRecord as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (InvalidInsightReference, IdempotencyConflict) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    response.status_code = stored_status
    return review


@router.post(
    "/insights/{insight_id}/delivery-receipts",
    response_model=DeliveryReceiptAccepted,
    status_code=status.HTTP_201_CREATED,
)
async def create_delivery_receipt(
    insight_id: str, body: DeliveryReceiptCreate, engine: PMEngine = Depends(get_engine)
) -> DeliveryReceiptAccepted:
    try:
        if body.state == "queued":
            from config import settings

            receipt = queue_delivery(
                _store(engine), settings.INTELLIGENCE_MODE, insight_id, body.revision, body.channel
            )
            if receipt is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="delivery_suppressed"
                )
        else:
            receipt = confirm_delivery(
                _store(engine), insight_id, body.revision, body.channel, body.state == "sent"
            )
    except MissingInsightRecord as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return DeliveryReceiptAccepted(**receipt)


@router.post("/insights/{insight_id}/feedback", response_model=InsightFeedbackAccepted)
async def create_insight_feedback(
    insight_id: str, body: InsightFeedbackCreate, engine: PMEngine = Depends(get_engine)
) -> InsightFeedbackAccepted:
    try:
        feedback = _store(engine).save_feedback(insight_id, body.revision, body.label)
    except MissingInsightRecord as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return InsightFeedbackAccepted(**feedback)


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
