import json

import pytest

from app.services.insight_budget import BudgetPolicy, BudgetService
from app.services.insight_triage import TriageBudgetDenied, triage_source, triage_with_reservation


class FixtureLLM:
    def __init__(self, payload):
        self.payload = payload

    async def complete(self, messages, **kwargs):
        return json.dumps(self.payload)


@pytest.mark.asyncio
async def test_triage_admits_relevant_evidence_with_a_meaningful_delta():
    result = await triage_source(
        question="What practical learning should be tested next?",
        title="A change",
        content="Evidence.",
        llm=FixtureLLM({
            "disposition": "admit", "relevance": "relevant",
            "novelty": "meaningful_delta", "reason": "New attributed mechanism.",
        }),
    )

    assert result.disposition == "admit"


@pytest.mark.asyncio
async def test_triage_never_admits_unchanged_or_irrelevant_evidence():
    result = await triage_source(
        question="What practical learning should be tested next?",
        title="An old item",
        content="Evidence.",
        llm=FixtureLLM({
            "disposition": "admit", "relevance": "irrelevant",
            "novelty": "unchanged", "reason": "No delta.",
        }),
    )

    assert result.disposition == "quiet_reference"


def _reservation(operation_id: str) -> dict:
    return {
        "operation_id": operation_id, "operation_type": "triage", "policy_revision": "fixture-v1",
        "provider": "fixture", "rate_revision": "fixture-rates", "maximum_micros": 10,
        "allowance_class": "sensing",
    }


@pytest.mark.asyncio
async def test_triage_does_not_call_the_model_when_reservation_is_denied(store_factory):
    class NoCallLLM:
        async def complete(self, messages, **kwargs):
            raise AssertionError("model must not be called after budget denial")

    budget = BudgetService(store_factory(), BudgetPolicy({"sensing": 0}, "fixture-rates"))
    with pytest.raises(TriageBudgetDenied):
        await triage_with_reservation(
            question="Question", title="Title", content="Evidence", llm=NoCallLLM(), budget=budget,
            reservation_payload=_reservation("denied"),
        )


@pytest.mark.asyncio
async def test_triage_finalizes_confirmed_usage_after_the_model_returns(store_factory):
    store = store_factory()
    budget = BudgetService(store, BudgetPolicy({"sensing": 10}, "fixture-rates"))
    result = await triage_with_reservation(
        question="Question", title="Title", content="Evidence", budget=budget, actual_micros=4,
        reservation_payload=_reservation("confirmed"),
        llm=FixtureLLM({
            "disposition": "admit", "relevance": "relevant",
            "novelty": "meaningful_delta", "reason": "New evidence.",
        }),
    )

    assert result.disposition == "admit"
    assert store.operational_summary()["cost_micros"]["finalized"] == 4
