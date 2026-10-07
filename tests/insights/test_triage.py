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
        return json.dumps(
            {
                "disposition": "quiet_reference",
                "relevance": "adjacent",
                "novelty": "unknown",
                "reason": "Recorded.",
            }
        )


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
async def test_triage_prompt_carries_interest_constraints_as_context():
    llm = RecordingLLM()
    await triage_source(
        question="What changed in enterprise mobile security?",
        constraints=["State the affected device and deployment scope."],
        title="Desktop browser patch",
        content="Windows, Mac, and Linux fixes.",
        llm=llm,
    )

    context = json.loads(llm.messages[1]["content"])
    assert context["constraints"] == ["State the affected device and deployment scope."]
    assert "Shared keywords do not establish" in llm.messages[0]["content"]


@pytest.mark.asyncio
async def test_triage_admits_relevant_evidence_with_a_meaningful_delta():
    result = await triage_source(
        question="What practical learning should be tested next?",
        title="A change",
        content="Evidence.",
        llm=FixtureLLM(
            {
                "disposition": "admit",
                "relevance": "relevant",
                "novelty": "meaningful_delta",
                "reason": "New attributed mechanism.",
            }
        ),
    )

    assert result.disposition == "admit"


@pytest.mark.asyncio
async def test_triage_never_admits_unchanged_or_irrelevant_evidence():
    result = await triage_source(
        question="What practical learning should be tested next?",
        title="An old item",
        content="Evidence.",
        llm=FixtureLLM(
            {
                "disposition": "admit",
                "relevance": "irrelevant",
                "novelty": "unchanged",
                "reason": "No delta.",
            }
        ),
    )

    assert result.disposition == "quiet_reference"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("relevance", "novelty", "expected"),
    [
        ("adjacent", "meaningful_delta", "quiet_reference"),
        ("relevant", "unknown", "defer"),
    ],
)
async def test_triage_does_not_admit_without_both_supported_conditions(
    relevance, novelty, expected
):
    result = await triage_source(
        question="What changed?",
        title="A candidate",
        content="Evidence.",
        llm=FixtureLLM(
            {
                "disposition": "admit",
                "relevance": relevance,
                "novelty": novelty,
                "reason": "Model requested admission.",
            }
        ),
    )

    assert result.disposition == expected


def _reservation(operation_id: str) -> dict[str, Any]:
    return {
        "operation_id": operation_id,
        "operation_type": "triage",
        "policy_revision": "fixture-v1",
        "provider": "fixture",
        "rate_revision": "fixture-rates",
        "maximum_micros": 10,
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
            question="Question",
            title="Title",
            content="Evidence",
            llm=NoCallLLM(),
            budget=budget,
            reservation_payload=_reservation("denied"),
        )


@pytest.mark.asyncio
async def test_triage_finalizes_confirmed_usage_after_the_model_returns(store_factory):
    store = store_factory()
    budget = BudgetService(store, BudgetPolicy({"sensing": 10}, "fixture-rates"))
    result = await triage_with_reservation(
        question="Question",
        title="Title",
        content="Evidence",
        budget=budget,
        actual_micros=4,
        reservation_payload=_reservation("confirmed"),
        llm=FixtureLLM(
            {
                "disposition": "admit",
                "relevance": "relevant",
                "novelty": "meaningful_delta",
                "reason": "New evidence.",
            }
        ),
    )

    assert result.disposition == "admit"
    assert store.operational_summary()["cost_micros"]["finalized"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "title",
    ["Android Security Bulletin—October 2026", "Samsung Security Maintenance Release October 2026"],
)
@pytest.mark.parametrize(
    "quote,content,expected",
    [
        (None, "Monthly patches include critical CVEs.", "quiet_reference"),
        (
            "Android devices are affected. CVE-2026-1234 is actively exploited.",
            "Android devices are affected. CVE-2026-1234 is actively exploited.",
            "admit",
        ),
        (
            "Android devices are affected.\nCVE-2026-1234 is actively exploited.",
            "Android devices are affected.\nCVE-2026-1234 is actively exploited.",
            "admit",
        ),
        (
            "Monthly patches include critical CVEs.",
            "Monthly patches include critical CVEs.",
            "quiet_reference",
        ),
        (
            "CVE-2026-1234 is actively exploited in the wild.",
            "CVE-2026-1234 is actively exploited in the wild.",
            "admit",
        ),
        (
            "No vulnerabilities are actively exploited.",
            "No vulnerabilities are actively exploited.",
            "quiet_reference",
        ),
        (
            "actively exploited",
            "There is no evidence that these vulnerabilities are actively exploited.",
            "quiet_reference",
        ),
        (
            "CVE-2026-1234 is actively exploited in the wild.",
            "Monthly patches include critical CVEs.",
            "quiet_reference",
        ),
    ],
)
async def test_monthly_bulletins_need_notable_original_evidence(title, quote, content, expected):
    payload = {
        "disposition": "admit",
        "relevance": "relevant",
        "novelty": "meaningful_delta",
        "reason": "Monthly bulletin.",
    }
    if quote is not None:
        payload["bulletin_notable_quote"] = quote
    result = await triage_source(
        question="What changed in enterprise mobile security?",
        title=title,
        content=content,
        llm=FixtureLLM(payload),
    )
    assert result.disposition == expected
