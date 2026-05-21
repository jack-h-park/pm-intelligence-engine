"""Unit tests for Stage 2 — Insight Extraction."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.stages import RunContext, S1OutputData, S2Input, S2Output
from app.stages import s2_insight


def _make_context() -> RunContext:
    return RunContext(
        run_id="test-run-id",
        product_id="test-product",
        pm_identity="PM identity text",
        company_context="Company context text",
        product_context="## Strategy Pillars\n1. Reduce attack surface\n2. Protect remaining attack surface",
    )


def _make_s1_output() -> S1OutputData:
    return S1OutputData(
        signal_id="sig-001",
        title="Android APM no admin enforcement",
        summary="Android 16 APM lacks admin enforcement API.",
        category="platform",
        source="https://example.com",
        quality_passed=True,
    )


def _make_store() -> MagicMock:
    store = MagicMock()
    store.save_stage_output = MagicMock(return_value="output-id")
    return store


def _make_llm_returning(data: dict) -> AsyncMock:
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(data))
    return llm


_VALID_S2_RESPONSE = {
    "what_changed": "Android 16 introduces APM but without admin enforcement.",
    "reframing": "Market frames this as consumer feature; for KPE Ultra it is a compliance gap.",
    "pillar_references": ["Reduce attack surface (Ingress & Egress)"],
    "relevance_explanation": "Aligns directly with Pillar 1 — attack surface reduction.",
    "suggested_mode": "evaluate",
    "suggestion_reasoning": "Signal is directly relevant to a named strategy pillar and warrants full persona evaluation.",
}


@pytest.mark.asyncio
async def test_s2_produces_valid_output():
    with patch("app.stages.s2_insight.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template text"
        out = await s2_insight.run(
            input=S2Input(signal_id="sig-001", s1_output=_make_s1_output(), product_id="test"),
            context=_make_context(),
            llm=_make_llm_returning(_VALID_S2_RESPONSE),
            store=_make_store(),
        )
    assert isinstance(out, S2Output)
    assert out.stage == "s2"
    assert len(out.output.pillar_references) >= 1
    assert out.output.what_changed


@pytest.mark.asyncio
async def test_s2_saves_output_to_store():
    store = _make_store()
    with patch("app.stages.s2_insight.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template text"
        await s2_insight.run(
            input=S2Input(signal_id="sig-001", s1_output=_make_s1_output(), product_id="test"),
            context=_make_context(),
            llm=_make_llm_returning(_VALID_S2_RESPONSE),
            store=store,
        )
    store.save_stage_output.assert_called_once()


@pytest.mark.asyncio
async def test_s2_strips_markdown_code_fences():
    wrapped = f"```json\n{json.dumps(_VALID_S2_RESPONSE)}\n```"
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=wrapped)
    with patch("app.stages.s2_insight.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template text"
        out = await s2_insight.run(
            input=S2Input(signal_id="sig-001", s1_output=_make_s1_output(), product_id="test"),
            context=_make_context(),
            llm=llm,
            store=_make_store(),
        )
    assert out.output.what_changed == _VALID_S2_RESPONSE["what_changed"]
