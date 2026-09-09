from concurrent.futures import ThreadPoolExecutor

from app.services.insight_budget import BudgetPolicy, BudgetService


def _policy() -> BudgetPolicy:
    return BudgetPolicy(
        allowances_micros={"sensing": 100},
        rate_revision="fixture-rates-v1",
    )


def _request(operation_id: str, maximum_micros: int = 100) -> dict:
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


def test_missing_or_changed_rate_revision_blocks_paid_reservation(store_factory):
    store = store_factory()
    budget = BudgetService(store, _policy())

    denied = budget.reserve({**_request("missing-rate"), "rate_revision": "other-rate"})

    assert denied.granted is False
    assert denied.code == "budget_denied"
