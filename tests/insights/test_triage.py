import json
from typing import Any, get_args

import pytest

from app.services.insight_budget import BudgetPolicy, BudgetService
from app.services.insight_triage import (
    TriageBudgetDenied,
    TriageDecision,
    triage_source,
    triage_with_reservation,
)


class FixtureLLM:
    def __init__(self, payload):
        self.payload = payload

    async def complete(self, messages, **kwargs):
        return json.dumps(self.payload)


class RecordingLLM:
    """Captures the prompt and returns a valid payload, so the prompt can be inspected."""

    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []

    async def complete(self, messages, **kwargs):
        self.messages = list(messages)
        return json.dumps({
            "disposition": "quiet_reference", "relevance": "adjacent",
            "novelty": "unknown", "reason": "Recorded.",
        })


@pytest.mark.asyncio
async def test_triage_prompt_names_every_field_and_value_the_schema_requires():
    """The prompt must state the schema it is validated against.

    Every other test here hands back a correct payload from a fixture, so none of them
    exercises the prompt. Run against three real models (27B, 120B and a 106B MoE) on
    2026-09-17, all three returned valid JSON with invented keys — `question_relevance`,
    `result`, `classification` — because the prompt only said "classify question
    relevance and evidence novelty". Every call failed validation, 0 of 20 each, and the
    repair retries could not help: the JSON parsed, it was the wrong JSON.
    """
    llm = RecordingLLM()
    await triage_source(question="Q?", title="T", content="C", llm=llm)
    prompt = " ".join(m["content"] for m in llm.messages)

    for field, annotation in TriageDecision.model_fields.items():
        assert field in prompt, f"prompt never names required field {field!r}"
        for value in get_args(annotation.annotation):
            assert value in prompt, f"prompt never names allowed {field} value {value!r}"


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


def _reservation(operation_id: str) -> dict[str, Any]:
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
