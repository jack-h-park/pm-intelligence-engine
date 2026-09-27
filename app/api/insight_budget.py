"""Authenticated reservation endpoints; denial always precedes paid work."""

import hashlib

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import get_engine
from app.factory import PMEngine
from app.models.insights import BudgetReservation
from app.services.insight_budget import BudgetPolicy, BudgetService, utc_day_window

router = APIRouter(prefix="/insight-budget", tags=["insight-budget"])


class ReservationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(min_length=1)
    operation_type: str = Field(min_length=1)
    candidate_id: str | None = None
    job_id: str | None = None
    policy_revision: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    rate_revision: str = Field(min_length=1)
    maximum_micros: int = Field(gt=0)
    allowance_class: str = Field(min_length=1)


class ReservationFinalize(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actual_micros: int | str


class SearchReservationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(min_length=1)
    interest_id: str = Field(min_length=1)
    query_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_revision: str = Field(min_length=1)
    rate_revision: str = Field(min_length=1)
    maximum_micros: int = Field(gt=0)


def _budget_service(engine: PMEngine) -> BudgetService:
    from config import settings

    if not settings.INSIGHT_WRITES_ENABLED or engine.insight_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Insight writes are disabled"
        )
    return BudgetService(
        engine.insight_store,
        BudgetPolicy(
            allowances_micros={
                "sensing": settings.INTELLIGENCE_SENSING_ALLOWANCE_MICROS or 0,
                "decision": settings.INTELLIGENCE_DECISION_ALLOWANCE_MICROS or 0,
            },
            rate_revision=settings.INTELLIGENCE_RATE_REVISION,
        ),
    )


@router.post(
    "/search-reservations", response_model=BudgetReservation,
    status_code=status.HTTP_201_CREATED,
)
async def reserve_primary_search(
    body: SearchReservationCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    engine: PMEngine = Depends(get_engine),
) -> BudgetReservation:
    """Reserve one Tavily call, with Engine-owned daily and per-call limits."""
    from config import settings

    if not idempotency_key or idempotency_key != body.operation_id:
        raise HTTPException(status_code=422, detail="Idempotency-Key must match operation_id")
    expected_id = "s2k-search:" + hashlib.sha256(
        f"{body.interest_id}\n{body.query_sha256}".encode()
    ).hexdigest()
    if body.operation_id != expected_id:
        raise HTTPException(status_code=422, detail="invalid search operation_id")
    allowance = settings.INTELLIGENCE_SEARCH_ALLOWANCE_MICROS or 0
    maximum = settings.INTELLIGENCE_SEARCH_MAXIMUM_MICROS or 0
    revision = settings.INTELLIGENCE_SEARCH_RATE_REVISION
    if (allowance <= 0 or maximum <= 0 or not revision
            or body.rate_revision != revision or body.maximum_micros > maximum):
        raise HTTPException(status_code=409, detail="budget_denied")
    if not settings.INSIGHT_WRITES_ENABLED or engine.insight_store is None:
        raise HTTPException(status_code=503, detail="Insight writes are disabled")
    store = engine.insight_store
    payload = {
        "operation_id": body.operation_id,
        "operation_type": "primary_source_search",
        "policy_revision": body.policy_revision,
        "provider": "tavily",
        "rate_revision": body.rate_revision,
        "maximum_micros": body.maximum_micros,
        "allowance_class": "search_acquisition",
        "budget_window": utc_day_window(),
    }
    reservation, created = store.reserve_budget_with_status(payload, allowance)
    if reservation is None:
        raise HTTPException(status_code=409, detail="budget_denied")
    if any(getattr(reservation, key) != value for key, value in payload.items()):
        raise HTTPException(status_code=409, detail="operation_id_conflict")
    if not created:
        response.status_code = status.HTTP_200_OK
    return reservation


@router.post("/reservations", response_model=BudgetReservation, status_code=status.HTTP_201_CREATED)
async def reserve(
    body: ReservationCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    engine: PMEngine = Depends(get_engine),
) -> BudgetReservation:
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Idempotency-Key is required",
        )
    decision = _budget_service(engine).reserve(body.model_dump())
    if not decision.granted:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="budget_denied")
    assert decision.reservation is not None, "a granted decision always carries a reservation"
    return decision.reservation


@router.post("/reservations/{reservation_id}/finalize", response_model=BudgetReservation)
async def finalize(
    reservation_id: str,
    body: ReservationFinalize,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    engine: PMEngine = Depends(get_engine),
) -> BudgetReservation:
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Idempotency-Key is required",
        )
    try:
        return _budget_service(engine).finalize(reservation_id, body.actual_micros)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
