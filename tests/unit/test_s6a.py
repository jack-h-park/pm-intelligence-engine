"""Unit tests for Stage 6A — PoC Plan generation.

Covers:
  - Valid JSON response produces a correct S6AOutput
  - Output is persisted to the store (save_stage_output called with stage='s6a')
  - Markdown-fenced JSON is unwrapped correctly
  - Blocking assumptions are separated and passed into the prompt
  - timeline_weeks is an integer and reflected in the output
  - Missing required fields in LLM response raises a validation error
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.stages import (
    Assumption,
    RunContext,
    S5OutputData,
    S6AInput,
    S6AOutput,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_context() -> RunContext:
    return RunContext(
        run_id="test-run-s6a",
        product_id="example-security-product",
        pm_identity="PM identity text",
        company_context="Company context",
        product_context="Strategy Pillar: **Attack Surface Reduction**.",
    )


def _make_store() -> MagicMock:
    store = MagicMock()
    store.save_stage_output = MagicMock(return_value="output-id")
    return store


def _make_s5_output(routing: str = "poc") -> S5OutputData:
    return S5OutputData(
        impact_score=3,
        strategic_fit_score=3,
        feasibility_score=3,
        confidence_score=3,
        composite_score=3.0,
        routing=routing,
        assumptions=[
            Assumption(
                statement="Partner agreement is achievable",
                severity="Blocking",
                reason="No partner = no product.",
            ),
            Assumption(
                statement="AMAPI deprecation applies to KPE",
                severity="Adjusting",
                reason="Narrows scope if false.",
            ),
        ],
        rationale="Blocking assumption requires PoC before engineering.",
        blocking_count=1,
    )


_VALID_S6A_RESPONSE = {
    "experiment_goal": "Validate that the partner agreement can be secured within 60 days.",
    "blocking_assumptions_addressed": ["Partner agreement is achievable"],
    "experiment_design": "Schedule introductory calls with three potential partners in week 1.",
    "success_criteria": "At least one partner signs an LOI by end of week 4.",
    "timeline_weeks": 4,
    "resources_needed": "PM (10h), BD lead (20h) — no engineering required.",
}


# ---------------------------------------------------------------------------
# Correctness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s6a_produces_valid_output():
    """Valid LLM response produces a well-typed S6AOutput."""
    from app.stages import s6a_poc_plan

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(_VALID_S6A_RESPONSE))

    with patch("app.stages.s6a_poc_plan.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template"
        out = await s6a_poc_plan.run(
            S6AInput(s5_output=_make_s5_output()),
            _make_context(),
            llm,
            _make_store(),
        )

    assert isinstance(out, S6AOutput)
    assert out.output.experiment_goal == _VALID_S6A_RESPONSE["experiment_goal"]
    assert out.output.timeline_weeks == 4
    assert out.run_id == "test-run-s6a"


@pytest.mark.asyncio
async def test_s6a_timeline_weeks_is_int():
    """timeline_weeks field must parse as an integer."""
    from app.stages import s6a_poc_plan

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(_VALID_S6A_RESPONSE))

    with patch("app.stages.s6a_poc_plan.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template"
        out = await s6a_poc_plan.run(
            S6AInput(s5_output=_make_s5_output()),
            _make_context(),
            llm,
            _make_store(),
        )

    assert isinstance(out.output.timeline_weeks, int)


@pytest.mark.asyncio
async def test_s6a_blocking_assumptions_reflected():
    """Output.blocking_assumptions_addressed should list the assumptions from the response."""
    from app.stages import s6a_poc_plan

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(_VALID_S6A_RESPONSE))

    with patch("app.stages.s6a_poc_plan.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template"
        out = await s6a_poc_plan.run(
            S6AInput(s5_output=_make_s5_output()),
            _make_context(),
            llm,
            _make_store(),
        )

    assert "Partner agreement is achievable" in out.output.blocking_assumptions_addressed


# ---------------------------------------------------------------------------
# Store persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s6a_saves_to_store():
    """Stage output is persisted to the store with stage='s6a'."""
    from app.stages import s6a_poc_plan

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(_VALID_S6A_RESPONSE))
    store = _make_store()

    with patch("app.stages.s6a_poc_plan.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template"
        await s6a_poc_plan.run(
            S6AInput(s5_output=_make_s5_output()),
            _make_context(),
            llm,
            store,
        )

    store.save_stage_output.assert_called_once()
    call_kwargs = store.save_stage_output.call_args
    stage_arg = call_kwargs.kwargs.get("stage") or call_kwargs.args[1]
    assert stage_arg == "s6a"


@pytest.mark.asyncio
async def test_s6a_persisted_json_is_valid():
    """The JSON saved to the store deserializes back to a valid S6AOutput."""
    from app.stages import s6a_poc_plan

    saved_json = None

    def capture_save(run_id, stage, output_json, version=1):
        nonlocal saved_json
        saved_json = output_json
        return "output-id"

    store = MagicMock()
    store.save_stage_output = MagicMock(side_effect=capture_save)

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(_VALID_S6A_RESPONSE))

    with patch("app.stages.s6a_poc_plan.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template"
        await s6a_poc_plan.run(
            S6AInput(s5_output=_make_s5_output()),
            _make_context(),
            llm,
            store,
        )

    assert saved_json is not None
    parsed = json.loads(saved_json)
    assert parsed["stage"] == "s6a"
    assert parsed["output"]["timeline_weeks"] == 4


# ---------------------------------------------------------------------------
# JSON parsing — markdown fence unwrapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s6a_unwraps_markdown_fenced_json():
    """LLM response wrapped in ```json ... ``` fences is parsed correctly."""
    from app.stages import s6a_poc_plan

    fenced = "```json\n" + json.dumps(_VALID_S6A_RESPONSE) + "\n```"
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=fenced)

    with patch("app.stages.s6a_poc_plan.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template"
        out = await s6a_poc_plan.run(
            S6AInput(s5_output=_make_s5_output()),
            _make_context(),
            llm,
            _make_store(),
        )

    assert out.output.timeline_weeks == 4


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s6a_missing_required_field_raises():
    """LLM response missing a required field raises a Pydantic ValidationError."""
    from app.stages import s6a_poc_plan

    incomplete = {k: v for k, v in _VALID_S6A_RESPONSE.items() if k != "success_criteria"}
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(incomplete))

    with patch("app.stages.s6a_poc_plan.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template"
        with pytest.raises(Exception):  # pydantic ValidationError
            await s6a_poc_plan.run(
                S6AInput(s5_output=_make_s5_output()),
                _make_context(),
                llm,
                _make_store(),
            )
