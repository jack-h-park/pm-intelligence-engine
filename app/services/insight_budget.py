"""Reservation-first budget policy for fixture and future paid operations."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.models.insights import BudgetReservation
from app.storage.insight_store import InsightStore


@dataclass(frozen=True)
class BudgetPolicy:
    allowances_micros: dict[str, int]
    rate_revision: str


@dataclass(frozen=True)
class BudgetDecision:
    granted: bool
    code: str
    reservation: BudgetReservation | None = None


def utc_day_window(now: datetime | None = None) -> str:
    """Return the stable UTC calendar-day key for a bounded operating allowance."""
    return (now or datetime.now(UTC)).astimezone(UTC).date().isoformat()


class BudgetService:
    def __init__(self, store: InsightStore, policy: BudgetPolicy) -> None:
        self._store = store
        self._policy = policy

    def reserve(self, payload: dict[str, Any]) -> BudgetDecision:
        allowance = self._policy.allowances_micros.get(payload.get("allowance_class", ""))
        if allowance is None or not self._policy.rate_revision:
            return BudgetDecision(False, "budget_denied")
        if payload.get("rate_revision") != self._policy.rate_revision:
            return BudgetDecision(False, "budget_denied")
        reservation = self._store.reserve_budget(payload, allowance)
        if reservation is None:
            return BudgetDecision(False, "budget_denied")
        return BudgetDecision(True, "reserved", reservation)

    def finalize(self, reservation_id: str, actual_micros: int | str) -> BudgetReservation:
        reservation = self._store.finalize_budget(reservation_id, actual_micros)
        if reservation is None:
            raise ValueError("reservation was not found")
        return reservation
