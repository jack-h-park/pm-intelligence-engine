"""Unit tests for Stage 1 — Signal Ingestion."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.models.stages import RunContext, S1Input, S1Output
from app.stages import s1_signal


def _make_context() -> RunContext:
    return RunContext(
        run_id="test-run-id",
        product_id="test-product",
        pm_identity="PM identity text",
        company_context="Company context text",
        product_context="Product context text",
    )


def _make_store() -> MagicMock:
    store = MagicMock()
    store.save_stage_output = MagicMock(return_value="output-id")
    return store


@pytest.mark.asyncio
async def test_s1_produces_valid_output():
    s1_input = S1Input(
        signal_id="sig-001",
        title="Test Signal Title",
        raw_content="This is the raw signal content with meaningful information about platform changes.",
        source_url="https://example.com/signal",
    )
    out = await s1_signal.run(
        input=s1_input,
        context=_make_context(),
        llm=AsyncMock(),
        store=_make_store(),
    )
    assert isinstance(out, S1Output)
    assert out.stage == "s1"
    assert out.output.signal_id == "sig-001"
    assert out.output.title == "Test Signal Title"
    assert out.output.quality_passed is True
    assert out.output.summary


@pytest.mark.asyncio
async def test_s1_saves_output_to_store():
    store = _make_store()
    s1_input = S1Input(
        signal_id="sig-002",
        title="Another Signal",
        raw_content="Content here.",
    )
    await s1_signal.run(
        input=s1_input,
        context=_make_context(),
        llm=AsyncMock(),
        store=store,
    )
    store.save_stage_output.assert_called_once()
    call_kwargs = store.save_stage_output.call_args
    assert call_kwargs.kwargs["stage"] == "s1" or call_kwargs.args[1] == "s1"


@pytest.mark.asyncio
async def test_s1_truncates_long_content():
    long_content = "word " * 300
    s1_input = S1Input(
        signal_id="sig-003",
        title="Long Signal",
        raw_content=long_content,
    )
    out = await s1_signal.run(
        input=s1_input,
        context=_make_context(),
        llm=AsyncMock(),
        store=_make_store(),
    )
    assert len(out.output.summary) <= 820


@pytest.mark.asyncio
async def test_s1_infers_regulation_category():
    s1_input = S1Input(
        signal_id="sig-004",
        title="DISA STIG Mandate Update",
        raw_content="DISA regulation compliance requirement for government devices.",
    )
    out = await s1_signal.run(
        input=s1_input,
        context=_make_context(),
        llm=AsyncMock(),
        store=_make_store(),
    )
    assert out.output.category == "regulation"
