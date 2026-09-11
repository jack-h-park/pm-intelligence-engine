"""Unit tests for Stage 3 — Opportunity Creation."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.decision_case import DecisionCase
from app.models.stages import RunContext, S2OutputData, S3Input, S3Output
from app.stages import s3_opportunity


def _make_context() -> RunContext:
    return RunContext(
        run_id="test-run-id",
        product_id="test-product",
        pm_identity="PM identity text",
        company_context="Company context text",
        product_context="Product context with strategy pillars.",
    )


@pytest.mark.asyncio
async def test_s3_includes_pinned_decision_case_without_rewriting_its_facts():
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(_VALID_S3_RESPONSE))
    context = _make_context().model_copy(
        update={
            "decision_case": DecisionCase(
                prepared_context_id="prepared-1",
                prepared_context_revision=1,
                product_id="test-product",
                decision_question="Should we retain the current policy?",
                input_origins=["direct"],
                hypotheses=["The behavior may be acceptable."],
                constraints=["Do not infer a customer need."],
                options=["Retain status quo"],
            )
        }
    )
    with patch("app.stages.s3_opportunity.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template text"
        await s3_opportunity.run(
            input=S3Input(signal_id="sig-001", s2_output=_make_s2_output(), product_id="test"),
            context=context,
            llm=llm,
            store=_make_store(),
        )

    prompt = llm.complete.call_args.kwargs["messages"][1]["content"]
    assert "Should we retain the current policy?" in prompt
    assert "Do not infer a customer need." in prompt
    assert "Retain status quo" in prompt


def _make_s2_output() -> S2OutputData:
    # `relevance_explanation` is the pre-`claims` field, auto-coerced into a
    # single inference claim by S2OutputData's backward-compat validator.
    return S2OutputData(  # type: ignore[call-arg]
        what_changed="Android 16 APM lacks admin enforcement API.",
        reframing="Consumer feature → compliance gap for the enterprise segment.",
        pillar_references=["Reduce attack surface (Ingress & Egress)"],
        relevance_explanation="Directly addresses Pillar 1.",
        relevance_score=4,
        suggested_mode="evaluate",
        suggestion_reasoning="Signal is directly relevant to a named strategy pillar.",
    )


def _make_store() -> MagicMock:
    store = MagicMock()
    store.save_stage_output = MagicMock(return_value="output-id")
    return store


_VALID_S3_RESPONSE = {
    "problem_statement": "The platform lacks admin enforcement for a security policy.",
    "target_user": "IT security admin at a regulated organization.",
    "hypothesis": (
        "If the platform exposes an admin-enforced policy, then IT admins will mandate it "
        "across their fleet, because admin-enforced posture is the only acceptable "
        "configuration in this segment."
    ),
    "assumed_value_user": (
        "Eliminates a compliance gap — the policy becomes admin-enforced, not user-optional."
    ),
    "assumed_value_business": "The platform ships enforcement ahead of the OS's native equivalent.",
}


@pytest.mark.asyncio
async def test_s3_produces_valid_output():
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(_VALID_S3_RESPONSE))
    with patch("app.stages.s3_opportunity.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template text"
        out = await s3_opportunity.run(
            input=S3Input(signal_id="sig-001", s2_output=_make_s2_output(), product_id="test"),
            context=_make_context(),
            llm=llm,
            store=_make_store(),
        )
    assert isinstance(out, S3Output)
    assert out.stage == "s3"
    assert out.output.hypothesis
    assert out.output.problem_statement
    assert out.output.target_user
    # value_horizon defaults to durable when the LLM omits it (US-41, backward-compatible)
    assert out.output.value_horizon == "durable"


@pytest.mark.asyncio
async def test_s3_value_horizon_transient_parsed():
    """When the LLM marks the opportunity transient, it flows into the output (US-41)."""
    resp = dict(_VALID_S3_RESPONSE, value_horizon="transient")
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(resp))
    with patch("app.stages.s3_opportunity.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template text"
        out = await s3_opportunity.run(
            input=S3Input(signal_id="sig-001", s2_output=_make_s2_output(), product_id="test"),
            context=_make_context(),
            llm=llm,
            store=_make_store(),
        )
    assert out.output.value_horizon == "transient"


@pytest.mark.asyncio
async def test_s3_hypothesis_is_falsifiable():
    from eval.rubrics import s3_hypothesis as rubric

    check = rubric.check(_VALID_S3_RESPONSE["hypothesis"])
    assert check.passed, f"Hypothesis failed rubric: {check.issues}"


@pytest.mark.asyncio
async def test_s3_saves_to_store():
    store = _make_store()
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(_VALID_S3_RESPONSE))
    with patch("app.stages.s3_opportunity.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template text"
        await s3_opportunity.run(
            input=S3Input(signal_id="sig-001", s2_output=_make_s2_output(), product_id="test"),
            context=_make_context(),
            llm=llm,
            store=store,
        )
    store.save_stage_output.assert_called_once()


@pytest.mark.asyncio
async def test_s3_writes_only_the_opportunity_memo_artifact():
    """No `checkpoint` artifact: it re-rendered the same S3OutputData the memo
    already carries, nothing ever read it back, and pipeline.py documents it as
    dropped. Guards against the write being reintroduced."""
    store = _make_store()
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(_VALID_S3_RESPONSE))
    with patch("app.stages.s3_opportunity.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template text"
        await s3_opportunity.run(
            input=S3Input(signal_id="sig-001", s2_output=_make_s2_output(), product_id="test"),
            context=_make_context(),
            llm=llm,
            store=store,
        )
    types = [c.kwargs.get("artifact_type") for c in store.save_artifact.call_args_list]
    assert types == ["opportunity_memo"]
