"""Unit tests for Stage 6B — PRD generation.

Covers:
  - Valid JSON response produces a correct S6BOutput
  - Completeness check is computed deterministically from the LLM output
  - Completeness score reflects actual field counts (not LLM-supplied)
  - rollout_phases is always False (not a field in the prompt)
  - Output is persisted to the store with stage='s6b'
  - Markdown-fenced JSON is unwrapped correctly
  - Missing required fields in LLM response raises a validation error
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.stages import (
    Assumption,
    RunContext,
    S5OutputData,
    S6BInput,
    S6BOutput,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_context() -> RunContext:
    return RunContext(
        run_id="test-run-s6b",
        product_id="example-security-product",
        pm_identity="PM identity text",
        company_context="Company context",
        product_context="Strategy Pillar: **Attack Surface Reduction**.",
    )


def _make_store() -> MagicMock:
    store = MagicMock()
    store.save_stage_output = MagicMock(return_value="output-id")
    return store


def _make_s5_output(routing: str = "prd") -> S5OutputData:
    return S5OutputData(
        impact_score=4,
        strategic_fit_score=5,
        feasibility_score=4,
        confidence_score=4,
        composite_score=4.30,
        routing=routing,
        assumptions=[
            Assumption(
                statement="Platform API deprecation timeline is confirmed",
                severity="Adjusting",
                reason="Affects urgency but not core value.",
            ),
        ],
        rationale="High composite score with no blocking assumptions.",
        blocking_count=0,
    )


_VALID_S6B_RESPONSE = {
    "problem_statement": "Admins cannot enforce the policy via the platform's existing tools.",
    "target_user": "IT security admin at a regulated organization running Android 16.",
    "success_metrics": [
        "Enforcement policy deployed in >80% of managed devices within 60 days of GA.",
        "Zero compliance violations related to the policy in audit reports within 90 days.",
    ],
    "user_stories": [
        "As an IT admin, I want to set APM as mandatory so that all fleet devices comply.",
        "As a security officer, I want to audit enforcement status so that I can generate compliance reports.",
        "As a platform developer, I want an API for the policy so that I can build admin enforcement into our MDM.",
    ],
    "in_scope": [
        "Platform enforcement policy API",
        "MDM integration hooks for status reporting",
    ],
    "out_of_scope": [
        "Consumer-facing UX changes",
        "Enforcement on devices outside the platform",
    ],
    "technical_dependencies": ["Android 16 admin API (released)", "the platform's firmware cycle"],
    "open_questions": ["What is the underlying platform API's deprecation date?"],
    "risks": ["A compliance standard may mandate a competing solution instead of this one."],
}


# ---------------------------------------------------------------------------
# Correctness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s6b_produces_valid_output():
    """Valid LLM response produces a well-typed S6BOutput."""
    from app.stages import s6b_prd

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(_VALID_S6B_RESPONSE))

    with patch("app.stages.s6b_prd.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template"
        out = await s6b_prd.run(
            S6BInput(s5_output=_make_s5_output()),
            _make_context(),
            llm,
            _make_store(),
        )

    assert isinstance(out, S6BOutput)
    assert out.run_id == "test-run-s6b"
    assert out.output.problem_statement == _VALID_S6B_RESPONSE["problem_statement"]
    assert len(out.output.user_stories) == 3


# ---------------------------------------------------------------------------
# Completeness check — computed deterministically
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s6b_completeness_computed_not_llm_supplied():
    """Completeness check is always computed from actual field counts, not LLM text."""
    from app.stages import s6b_prd

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(_VALID_S6B_RESPONSE))

    with patch("app.stages.s6b_prd.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template"
        out = await s6b_prd.run(
            S6BInput(s5_output=_make_s5_output()),
            _make_context(),
            llm,
            _make_store(),
        )

    c = out.output.completeness
    # Fields with sufficient entries must be True
    assert c.problem_statement is True
    assert c.target_user is True
    assert c.success_metrics is True       # >= 2 entries
    assert c.user_stories is True          # >= 3 entries
    assert c.out_of_scope is True          # >= 2 entries
    assert c.risks is True
    # rollout_phases is always False (not asked in prompt)
    assert c.rollout_phases is False


@pytest.mark.asyncio
async def test_s6b_completeness_score_reflects_true_count():
    """Completeness.score equals the count of True fields."""
    from app.stages import s6b_prd

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(_VALID_S6B_RESPONSE))

    with patch("app.stages.s6b_prd.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template"
        out = await s6b_prd.run(
            S6BInput(s5_output=_make_s5_output()),
            _make_context(),
            llm,
            _make_store(),
        )

    c = out.output.completeness
    expected_score = sum(1 for v in c.model_dump().values() if v)
    assert c.score == expected_score


@pytest.mark.asyncio
async def test_s6b_completeness_penalizes_insufficient_user_stories():
    """Fewer than 3 user_stories makes completeness.user_stories=False."""
    from app.stages import s6b_prd

    sparse = dict(_VALID_S6B_RESPONSE)
    sparse["user_stories"] = [
        "As an admin, I want to set APM so that devices comply.",
        "As a developer, I want an API so that I can integrate.",
    ]  # only 2 — below the >= 3 threshold

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(sparse))

    with patch("app.stages.s6b_prd.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template"
        out = await s6b_prd.run(
            S6BInput(s5_output=_make_s5_output()),
            _make_context(),
            llm,
            _make_store(),
        )

    assert out.output.completeness.user_stories is False


@pytest.mark.asyncio
async def test_s6b_completeness_penalizes_single_out_of_scope():
    """Fewer than 2 out_of_scope entries makes completeness.out_of_scope=False."""
    from app.stages import s6b_prd

    sparse = dict(_VALID_S6B_RESPONSE)
    sparse["out_of_scope"] = ["Consumer-facing APM UX changes"]  # only 1

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(sparse))

    with patch("app.stages.s6b_prd.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template"
        out = await s6b_prd.run(
            S6BInput(s5_output=_make_s5_output()),
            _make_context(),
            llm,
            _make_store(),
        )

    assert out.output.completeness.out_of_scope is False
    assert out.output.completeness.non_goals is False


# ---------------------------------------------------------------------------
# Store persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s6b_saves_to_store():
    """Stage output is persisted to the store with stage='s6b'."""
    from app.stages import s6b_prd

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(_VALID_S6B_RESPONSE))
    store = _make_store()

    with patch("app.stages.s6b_prd.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template"
        await s6b_prd.run(
            S6BInput(s5_output=_make_s5_output()),
            _make_context(),
            llm,
            store,
        )

    store.save_stage_output.assert_called_once()
    call_kwargs = store.save_stage_output.call_args
    stage_arg = call_kwargs.kwargs.get("stage") or call_kwargs.args[1]
    assert stage_arg == "s6b"


@pytest.mark.asyncio
async def test_s6b_persisted_json_roundtrip():
    """The JSON saved to the store deserializes to a valid S6BOutput with completeness."""
    from app.stages import s6b_prd

    saved_json = None

    def capture_save(run_id, stage, output_json, version=1):
        nonlocal saved_json
        saved_json = output_json
        return "output-id"

    store = MagicMock()
    store.save_stage_output = MagicMock(side_effect=capture_save)

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(_VALID_S6B_RESPONSE))

    with patch("app.stages.s6b_prd.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template"
        await s6b_prd.run(
            S6BInput(s5_output=_make_s5_output()),
            _make_context(),
            llm,
            store,
        )

    assert saved_json is not None
    parsed = json.loads(saved_json)
    assert parsed["stage"] == "s6b"
    assert "completeness" in parsed["output"]
    assert isinstance(parsed["output"]["completeness"]["rollout_phases"], bool)


# ---------------------------------------------------------------------------
# JSON parsing — markdown fence unwrapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s6b_unwraps_markdown_fenced_json():
    """LLM response wrapped in ```json ... ``` fences is parsed correctly."""
    from app.stages import s6b_prd

    fenced = "```json\n" + json.dumps(_VALID_S6B_RESPONSE) + "\n```"
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=fenced)

    with patch("app.stages.s6b_prd.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template"
        out = await s6b_prd.run(
            S6BInput(s5_output=_make_s5_output()),
            _make_context(),
            llm,
            _make_store(),
        )

    assert out.output.problem_statement == _VALID_S6B_RESPONSE["problem_statement"]


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s6b_missing_required_field_raises():
    """LLM response missing a required field raises an error."""
    from app.stages import s6b_prd

    incomplete = {k: v for k, v in _VALID_S6B_RESPONSE.items() if k != "problem_statement"}
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(incomplete))

    with patch("app.stages.s6b_prd.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template"
        with pytest.raises(Exception):  # pydantic ValidationError
            await s6b_prd.run(
                S6BInput(s5_output=_make_s5_output()),
                _make_context(),
                llm,
                _make_store(),
            )
