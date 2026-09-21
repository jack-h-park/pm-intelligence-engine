from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.insight_budget import BudgetPolicy, BudgetService, utc_day_window


def _policy() -> BudgetPolicy:
    return BudgetPolicy(
        allowances_micros={"sensing": 100},
        rate_revision="fixture-rates-v1",
    )


def _request(operation_id: str, maximum_micros: int = 100) -> dict[str, Any]:
    return {
        "operation_id": operation_id,
        "operation_type": "fixture_analysis",
        "policy_revision": "fixture-policy-v1",
        "provider": "fixture-provider",
        "rate_revision": "fixture-rates-v1",
        "maximum_micros": maximum_micros,
        "allowance_class": "sensing",
    }


def test_competing_reservations_cannot_exceed_the_last_allowance(store_factory):
    store = store_factory()
    budget = BudgetService(store, _policy())

    with ThreadPoolExecutor(max_workers=2) as executor:
        operations = ["one", "two"]
        outcomes = list(
            executor.map(lambda operation: budget.reserve(_request(operation)), operations)
        )

    assert sum(outcome.granted for outcome in outcomes) == 1
    assert {outcome.code for outcome in outcomes} == {"reserved", "budget_denied"}


def test_unknown_usage_stays_encumbered_across_restart(store_factory):
    store = store_factory()
    budget = BudgetService(store, _policy())
    reservation = budget.reserve(_request("unknown"))
    assert reservation.granted is True

    finalized = budget.finalize(reservation.reservation.reservation_id, "unknown")
    restarted = BudgetService(store_factory(), _policy())
    denied = restarted.reserve(_request("later", maximum_micros=1))

    assert finalized.state == "unknown"
    assert denied.code == "budget_denied"


def test_unknown_usage_is_encumbered_only_inside_its_budget_window(store_factory):
    store = store_factory()
    budget = BudgetService(store, _policy())
    first = budget.reserve({**_request("first"), "budget_window": "2026-09-21"})
    assert first.granted is True
    budget.finalize(first.reservation.reservation_id, "unknown")

    same_window = budget.reserve(
        {**_request("same-window", maximum_micros=1), "budget_window": "2026-09-21"}
    )
    next_window = budget.reserve(
        {**_request("next-window"), "budget_window": "2026-09-22"}
    )

    assert same_window.code == "budget_denied"
    assert next_window.granted is True


def test_finalized_usage_counts_actual_cost_inside_its_budget_window(store_factory):
    store = store_factory()
    budget = BudgetService(store, _policy())
    first = budget.reserve(
        {**_request("actual-first"), "budget_window": "2026-09-21"}
    )
    assert first.granted is True
    budget.finalize(first.reservation.reservation_id, 30)

    second = budget.reserve(
        {**_request("actual-second", maximum_micros=70), "budget_window": "2026-09-21"}
    )
    third = budget.reserve(
        {**_request("actual-third", maximum_micros=1), "budget_window": "2026-09-21"}
    )

    assert second.granted is True
    assert third.code == "budget_denied"


def test_utc_day_window_uses_utc_at_a_local_day_boundary():
    local_evening = datetime(2026, 9, 21, 20, tzinfo=timezone(timedelta(hours=-7)))

    assert utc_day_window(local_evening) == "2026-09-22"


def test_missing_or_changed_rate_revision_blocks_paid_reservation(store_factory):
    store = store_factory()
    budget = BudgetService(store, _policy())

    denied = budget.reserve({**_request("missing-rate"), "rate_revision": "other-rate"})

    assert denied.granted is False
    assert denied.code == "budget_denied"
