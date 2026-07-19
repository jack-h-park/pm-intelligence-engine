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
    "relevance_score": 4,
    "suggested_mode": "evaluate",
    "suggestion_reasoning": "Signal is directly relevant to a named strategy pillar and warrants full persona evaluation.",
}

_LOW_RELEVANCE_S2_RESPONSE = {
    "what_changed": "A consumer app added dark mode support.",
    "reframing": "Market frames this as a UX improvement; no implication for enterprise security.",
    "pillar_references": [],
    "relevance_explanation": "This signal has no connection to Knox enterprise security.",
    "relevance_score": 1,
    "suggested_mode": "file",
    "suggestion_reasoning": "Signal is consumer-focused with no actionable implication for this product.",
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


@pytest.mark.asyncio
async def test_s2_includes_relevance_score():
    """relevance_score is present in output and matches LLM response."""
    with patch("app.stages.s2_insight.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template text"
        out = await s2_insight.run(
            input=S2Input(signal_id="sig-001", s1_output=_make_s1_output(), product_id="test"),
            context=_make_context(),
            llm=_make_llm_returning(_VALID_S2_RESPONSE),
            store=_make_store(),
        )
    assert out.output.relevance_score == 4
    assert 1 <= out.output.relevance_score <= 5


_CLAIMS_S2_RESPONSE = {
    "what_changed": "Android 16 introduces APM but without admin enforcement.",
    "reframing": "Market frames this as consumer feature; for KPE Ultra it is a compliance gap.",
    "pillar_references": ["Reduce attack surface (Ingress & Egress)"],
    "claims": [
        {"text": "APM ships with no admin enforcement API.", "source": "signal", "grounds": []},
        {"text": "KPE Ultra's Pillar 1 is attack-surface reduction.", "source": "product_context", "grounds": []},
        {"text": "The missing API blocks enforcing Pillar 1 on managed fleets.", "source": "inference", "grounds": [1, 2]},
    ],
    "relevance_score": 4,
    "suggested_mode": "evaluate",
    "suggestion_reasoning": "Directly relevant to a named pillar; warrants full evaluation.",
}


@pytest.mark.asyncio
async def test_s2_claims_flow_through_and_flatten():
    """New-shape claims survive the round-trip and the back-compat property flattens them."""
    with patch("app.stages.s2_insight.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template text"
        out = await s2_insight.run(
            input=S2Input(signal_id="sig-001", s1_output=_make_s1_output(), product_id="test"),
            context=_make_context(),
            llm=_make_llm_returning(_CLAIMS_S2_RESPONSE),
            store=_make_store(),
        )
    claims = out.output.claims
    assert [c.source for c in claims] == ["signal", "product_context", "inference"]
    assert claims[2].grounds == [1, 2]
    # back-compat: relevance_explanation property joins claim texts for downstream readers
    assert "blocks enforcing Pillar 1" in out.output.relevance_explanation
    # claims field serializes; legacy field does not
    dumped = json.loads(out.model_dump_json())["output"]
    assert "claims" in dumped and "relevance_explanation" not in dumped


@pytest.mark.asyncio
async def test_s2_insight_memo_renders_provenance_tags():
    """The Insight Memo tags each claim with its source and traces inference grounds."""
    store = _make_store()
    with patch("app.stages.s2_insight.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template text"
        await s2_insight.run(
            input=S2Input(signal_id="sig-001", s1_output=_make_s1_output(), product_id="test"),
            context=_make_context(),
            llm=_make_llm_returning(_CLAIMS_S2_RESPONSE),
            store=store,
        )
    memo = next(
        c.kwargs["content_md"]
        for c in store.save_artifact.call_args_list
        if c.kwargs.get("artifact_type") == "insight_memo"
    )
    assert "1. [signal]" in memo
    assert "2. [context]" in memo
    assert "3. [inferred ← 1, 2]" in memo


@pytest.mark.asyncio
async def test_s2_low_relevance_score_sets_archive_mode():
    """relevance 1 → suggested_mode == 'archive' (legacy 'file' normalized — US-43)."""
    with patch("app.stages.s2_insight.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template text"
        out = await s2_insight.run(
            input=S2Input(signal_id="sig-001", s1_output=_make_s1_output(), product_id="test"),
            context=_make_context(),
            llm=_make_llm_returning(_LOW_RELEVANCE_S2_RESPONSE),
            store=_make_store(),
        )
    assert out.output.relevance_score == 1
    assert out.output.suggested_mode == "archive"  # fixture sends legacy "file"; normalized


@pytest.mark.asyncio
async def test_s2_writes_only_the_insight_memo_artifact():
    """No `checkpoint` artifact — see test_s3 counterpart for the rationale."""
    store = _make_store()
    with patch("app.stages.s2_insight.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template text"
        await s2_insight.run(
            input=S2Input(signal_id="sig-001", s1_output=_make_s1_output(), product_id="test"),
            context=_make_context(),
            llm=_make_llm_returning(_CLAIMS_S2_RESPONSE),
            store=store,
        )
    types = [c.kwargs.get("artifact_type") for c in store.save_artifact.call_args_list]
    assert types == ["insight_memo"]
