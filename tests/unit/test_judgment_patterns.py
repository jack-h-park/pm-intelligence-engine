"""The judgment pack reaches the deciding stages, and only when it exists."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.models.stages import S3Input
from app.services.context_loader import JUDGMENT_PATTERNS_PATH, ContextLoader
from app.stages import s3_opportunity
from app.stages.judgment import judgment_patterns_block
from tests.unit.test_s3 import _VALID_S3_RESPONSE, _make_context, _make_s2_output, _make_store

PACK = "## Reversibility\nReversible calls deserve speed; positioning claims do not unwind."


def test_loader_returns_empty_when_the_checkout_has_no_pack(tmp_path: Path):
    assert ContextLoader(str(tmp_path)).load_judgment_patterns() == ""


def test_loader_reads_the_pack_when_present(tmp_path: Path):
    path = tmp_path / JUDGMENT_PATTERNS_PATH
    path.parent.mkdir(parents=True)
    path.write_text(PACK + "\n\n", encoding="utf-8")
    assert ContextLoader(str(tmp_path)).load_judgment_patterns() == PACK


def test_block_is_empty_without_a_pack():
    assert judgment_patterns_block(_make_context()) == ""


async def _s3_system_message(context) -> str:
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(_VALID_S3_RESPONSE))
    with patch("app.stages.s3_opportunity.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template text"
        await s3_opportunity.run(
            input=S3Input(signal_id="sig-001", s2_output=_make_s2_output(), product_id="test"),
            context=context,
            llm=llm,
            store=_make_store(),
        )
    return llm.complete.call_args.kwargs["messages"][0]["content"]


@pytest.mark.asyncio
async def test_s3_system_message_is_unchanged_without_a_pack():
    system = await _s3_system_message(_make_context())
    assert system.endswith("PM identity text")
    assert "Judgment Patterns" not in system


@pytest.mark.asyncio
async def test_s3_system_message_carries_the_pack_after_the_identity():
    context = _make_context().model_copy(update={"judgment_patterns": PACK})
    system = await _s3_system_message(context)
    assert system.index("PM identity text") < system.index("## Judgment Patterns")
    assert PACK in system


@pytest.mark.asyncio
async def test_s5_system_message_carries_the_pack_and_routing_is_unchanged():
    """The pack informs the S5 rationale; routing stays the deterministic rule."""
    from app.models.stages import S5Input
    from app.stages import s5_prioritization
    from tests.unit.test_s5 import _LLM_RESPONSE_NO_BLOCKING, _make_s4_output
    from tests.unit.test_s5 import _make_context as _s5_context
    from tests.unit.test_s5 import _make_store as _s5_store

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=_LLM_RESPONSE_NO_BLOCKING)
    context = _s5_context().model_copy(update={"judgment_patterns": PACK})
    with patch("app.stages.s5_prioritization.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template"
        output = await s5_prioritization.run(
            S5Input(s4_output=_make_s4_output(explorer=4, strategist=5, builder=4, skeptic=4)),
            context,
            llm,
            _s5_store(),
        )

    assert PACK in llm.complete.call_args.kwargs["messages"][0]["content"]
    assert output.output.routing == "prd"
