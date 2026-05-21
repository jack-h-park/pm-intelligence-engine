"""Unit tests for Stage 3 — Opportunity Creation."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

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


def _make_s2_output() -> S2OutputData:
    return S2OutputData(
        what_changed="Android 16 APM lacks admin enforcement API.",
        reframing="Consumer feature → compliance gap for KPE Ultra segment.",
        pillar_references=["Reduce attack surface (Ingress & Egress)"],
        relevance_explanation="Directly addresses Pillar 1.",
        suggested_mode="evaluate",
        suggestion_reasoning="Signal is directly relevant to a named strategy pillar.",
    )


def _make_store() -> MagicMock:
    store = MagicMock()
    store.save_stage_output = MagicMock(return_value="output-id")
    return store


_VALID_S3_RESPONSE = {
    "problem_statement": "KPE Ultra lacks admin enforcement for APM.",
    "target_user": "IT security admin at US government agency running KPE Ultra.",
    "hypothesis": "If Knox exposes an APM enforcement policy, then government admins will mandate APM across their fleet, because admin-enforced posture is the only acceptable security configuration in this segment.",
    "assumed_value_user": "Eliminates compliance gap — APM becomes admin-enforced, not user-optional.",
    "assumed_value_business": "Knox ships APM enforcement before Google's native AMAPI solution.",
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
