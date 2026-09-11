"""Authenticated reservation endpoints; denial always precedes paid work."""

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import get_engine
from app.factory import PMEngine
from app.models.insights import BudgetReservation
from app.services.insight_budget import BudgetPolicy, BudgetService

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
