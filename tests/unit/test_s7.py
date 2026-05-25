"""Unit tests for Stage 7 — Executive Summary generation.

Covers:
  - Full 'decide' mode produces S7Output with all four narrative fields
  - Partial 'brief' mode uses the partial JSON schema (no s5/s6 sections)
  - Output is saved as StageOutput (save_stage_output called with stage='s7')
  - Output is saved as Artifact (save_artifact called with type='executive_summary')
  - Markdown content is preserved exactly from the LLM response
  - Prior stage outputs (S1-S4) are loaded from the store
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
    S6AOutputData,
    S6BOutputData,
    S7Input,
    S7Output,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_context() -> RunContext:
    return RunContext(
        run_id="test-run-s7",
        product_id="samsung-knox-lockdown-mode",
        pm_identity="PM identity text",
        company_context="Company context",
        product_context="Strategy Pillar: **Attack Surface Reduction**.",
    )


def _make_store(has_prior_stages: bool = False) -> MagicMock:
    store = MagicMock()
    store.save_stage_output = MagicMock(return_value="output-id")
    store.save_artifact = MagicMock(return_value="artifact-id")

    if has_prior_stages:
        _meta = {"created_at": "2026-05-24T00:00:00", "model_used": "claude"}

        def _get_stage_output(run_id, stage, _version=None):  # noqa: ANN001
            data = {
                "s1": {
                    "output_json": json.dumps({
                        "stage": "s1", "run_id": run_id, "version": 1,
                        "output": {
                            "signal_id": "sig-test-001",
                            "title": "Android 16 NFC allowlist",
                            "category": "platform",
                            "summary": "Android 16 introduces native NFC admin allowlist.",
                            "source": "Android Developers Blog",
                            "event_date": None,
                            "quality_passed": True,
                        },
                        "metadata": _meta,
                    })
                },
                "s2": {
                    "output_json": json.dumps({
                        "stage": "s2", "run_id": run_id, "version": 1,
                        "output": {
                            "what_changed": "NFC admin allowlist is now natively supported.",
                            "reframing": "Knox can offer first-class NFC control before AMAPI.",
                            "relevance_explanation": (
                                "Directly addresses Attack Surface Reduction pillar."
                            ),
                            "relevance_score": 5,
                            "pillar_references": ["Attack Surface Reduction"],
                            "suggested_mode": "decide",
                            "suggestion_reasoning": "High relevance, proceed to full workflow.",
                        },
                        "metadata": _meta,
                    })
                },
                "s3": {
                    "output_json": json.dumps({
                        "stage": "s3", "run_id": run_id, "version": 1,
                        "output": {
                            "problem_statement": "Knox lacks native NFC admin control.",
                            "target_user": "IT admin at enterprise running KPE Ultra.",
                            "hypothesis": (
                                "If Knox exposes NFC allowlist, then admins will mandate it."
                            ),
                            "assumed_value_user": (
                                "Full NFC policy control without MDM workarounds."
                            ),
                            "assumed_value_business": "Knox ships before AMAPI deprecation.",
                        },
                        "metadata": _meta,
                    })
                },
                "s4": {
                    "output_json": json.dumps({
                        "stage": "s4", "run_id": run_id, "version": 1,
                        "output": {
                            "personas": [
                                {
                                    "persona": "explorer",
                                    "dimension": "Impact",
                                    "score": 5,
                                    "key_argument": "Strong market signal.",
                                    "open_question": "Timeline?",
                                },
                                {
                                    "persona": "strategist",
                                    "dimension": "Strategic Fit",
                                    "score": 5,
                                    "key_argument": "Perfect fit.",
                                    "open_question": "Risk?",
                                },
                                {
                                    "persona": "builder",
                                    "dimension": "Feasibility",
                                    "score": 4,
                                    "key_argument": "Feasible.",
                                    "open_question": "Dependencies?",
                                },
                                {
                                    "persona": "skeptic",
                                    "dimension": "Confidence",
                                    "score": 4,
                                    "key_argument": "No major blockers.",
                                    "open_question": "Adoption?",
                                },
                            ],
                            "rubric": {
                                "total_score": 11,
                                "score_grounding": 3,
                                "skeptic_quality": 3,
                                "open_question_quality": 3,
                                "persona_independence": 2,
                                "passed": True,
                                "issues": [],
                            },
                        },
                        "metadata": _meta,
                    })
                },
            }
            return data.get(stage)
        store.get_stage_output = MagicMock(side_effect=_get_stage_output)
    else:
        store.get_stage_output = MagicMock(return_value=None)

    return store


def _make_s5_output() -> S5OutputData:
    return S5OutputData(
        impact_score=5,
        strategic_fit_score=5,
        feasibility_score=4,
        confidence_score=4,
        composite_score=4.65,
        routing="prd",
        assumptions=[
            Assumption(
                statement="AMAPI NFC deprecation applies to KPE Ultra",
                severity="Informing",
                reason="Narrows urgency if false.",
            ),
        ],
        rationale="Highest composite score in the NFC cluster. PRD track appropriate.",
        blocking_count=0,
    )


def _make_s6b_output() -> S6BOutputData:
    from app.models.stages import PRDCompletenessCheck
    return S6BOutputData(
        problem_statement="Knox lacks native NFC admin control.",
        target_user="IT admin at enterprise running KPE Ultra.",
        success_metrics=["NFC policy deployed on 80% of devices within 60 days."],
        user_stories=[
            "As an IT admin, I want to allowlist NFC apps so devices comply.",
            "As a developer, I want an API so I can enforce NFC policy.",
            "As a security officer, I want audit reports for NFC compliance.",
        ],
        in_scope=["NFC allowlist API", "MDM integration"],
        out_of_scope=["Consumer NFC UX", "Non-Knox devices"],
        technical_dependencies=["Android 16 NFC admin API"],
        open_questions=["AMAPI deprecation date? (owner: Platform PM)"],
        risks=["AMAPI may not deprecate NFC APIs on schedule."],
        completeness=PRDCompletenessCheck(
            problem_statement=True, target_user=True, hypothesis=True,
            success_metrics=True, user_stories=True, in_scope=True,
            out_of_scope=True, technical_dependencies=True, open_questions=True,
            non_goals=True, rollout_phases=False, risks=True,
        ),
    )


_VALID_S7_FULL_RESPONSE = {
    "what_we_saw": (
        "Android 16 introduced a native NFC admin allowlist, "
        "a platform change Knox does not yet support."
    ),
    "what_it_means": (
        "This creates an enterprise compliance gap in the Attack Surface Reduction pillar."
    ),
    "what_we_decided": (
        "Composite score 4.65/5.00. Routed to PRD track. No blocking assumptions."
    ),
    "what_we_will_do_next": (
        "PRD track: 3 user stories, 2 success metrics. Engineering kickoff Q3."
    ),
    "markdown": (
        "# Executive Summary\n\n## Run Summary\n"
        "| Field | Value |\n|---|---|\n"
        "| Signal | Android 16 NFC allowlist |\n| Routing | PRD |"
    ),
}

_VALID_S7_PARTIAL_RESPONSE = {
    "what_we_saw": "Android 16 introduced a native NFC admin allowlist.",
    "what_it_means": "This may affect the Knox admin model.",
    "what_we_decided": "Filed as brief insight — no actionable opportunity identified at this time.",
    "what_we_will_do_next": "No further action required.",
    "markdown": "# Brief Summary\n\nFiled for reference.",
}


# ---------------------------------------------------------------------------
# Full 'decide' mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s7_decide_mode_produces_valid_output():
    """decide mode with S5+S6B inputs produces a well-typed S7Output."""
    from app.stages import s7_summary

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(_VALID_S7_FULL_RESPONSE))
    store = _make_store(has_prior_stages=True)

    with patch("app.stages.s7_summary.TemplateService") as mock_ts:
        mock_ts.return_value.load_template.return_value = "template"
        out = await s7_summary.run(
            S7Input(mode="decide", s5_output=_make_s5_output(), s6b_output=_make_s6b_output()),
            _make_context(),
            llm,
            store,
        )

    assert isinstance(out, S7Output)
    assert out.run_id == "test-run-s7"
    assert out.output.what_we_saw == _VALID_S7_FULL_RESPONSE["what_we_saw"]
    assert out.output.markdown.startswith("# Executive Summary")


@pytest.mark.asyncio
async def test_s7_decide_mode_with_s6a_poc_track():
    """decide mode with S6A (PoC track) produces a valid summary."""
    from app.stages import s7_summary

    s6a = S6AOutputData(
        experiment_goal="Validate partner agreement feasibility.",
        blocking_assumptions_addressed=["Partner agreement is achievable"],
        experiment_design="Schedule partner calls in week 1.",
        success_criteria="LOI signed by end of week 4.",
        timeline_weeks=4,
        resources_needed="PM 10h, BD lead 20h.",
    )

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(_VALID_S7_FULL_RESPONSE))
    store = _make_store(has_prior_stages=True)

    with patch("app.stages.s7_summary.TemplateService") as mock_ts:
        mock_ts.return_value.load_template.return_value = "template"
        out = await s7_summary.run(
            S7Input(mode="decide", s5_output=_make_s5_output(), s6a_output=s6a),
            _make_context(),
            llm,
            store,
        )

    assert isinstance(out, S7Output)
    assert out.output.what_we_decided is not None


# ---------------------------------------------------------------------------
# Partial modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s7_brief_mode_produces_partial_summary():
    """brief mode (no S5/S6) still produces a valid S7Output."""
    from app.stages import s7_summary

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(_VALID_S7_PARTIAL_RESPONSE))
    store = _make_store(has_prior_stages=False)

    with patch("app.stages.s7_summary.TemplateService") as mock_ts:
        mock_ts.return_value.load_template.return_value = "template"
        out = await s7_summary.run(
            S7Input(mode="brief"),
            _make_context(),
            llm,
            store,
        )

    assert isinstance(out, S7Output)
    assert "brief" in out.output.what_we_decided.lower() or out.output.what_we_decided


@pytest.mark.asyncio
async def test_s7_opportunity_mode():
    """opportunity mode (S5/S6 absent) still produces a valid S7Output."""
    from app.stages import s7_summary

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(_VALID_S7_PARTIAL_RESPONSE))
    store = _make_store(has_prior_stages=False)

    with patch("app.stages.s7_summary.TemplateService") as mock_ts:
        mock_ts.return_value.load_template.return_value = "template"
        out = await s7_summary.run(
            S7Input(mode="opportunity"),
            _make_context(),
            llm,
            store,
        )

    assert isinstance(out, S7Output)


# ---------------------------------------------------------------------------
# Store persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s7_saves_stage_output():
    """Stage output is persisted to the store with stage='s7'."""
    from app.stages import s7_summary

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(_VALID_S7_FULL_RESPONSE))
    store = _make_store()

    with patch("app.stages.s7_summary.TemplateService") as mock_ts:
        mock_ts.return_value.load_template.return_value = "template"
        await s7_summary.run(
            S7Input(mode="decide", s5_output=_make_s5_output(), s6b_output=_make_s6b_output()),
            _make_context(),
            llm,
            store,
        )

    store.save_stage_output.assert_called_once()
    call_kwargs = store.save_stage_output.call_args
    stage_arg = call_kwargs.kwargs.get("stage") or call_kwargs.args[1]
    assert stage_arg == "s7"


@pytest.mark.asyncio
async def test_s7_saves_artifact():
    """Executive summary is saved as an Artifact with type='executive_summary'."""
    from app.stages import s7_summary

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(_VALID_S7_FULL_RESPONSE))
    store = _make_store()

    with patch("app.stages.s7_summary.TemplateService") as mock_ts:
        mock_ts.return_value.load_template.return_value = "template"
        await s7_summary.run(
            S7Input(mode="decide", s5_output=_make_s5_output(), s6b_output=_make_s6b_output()),
            _make_context(),
            llm,
            store,
        )

    store.save_artifact.assert_called_once()
    call_kwargs = store.save_artifact.call_args
    artifact_type = call_kwargs.kwargs.get("artifact_type") or call_kwargs.args[1]
    assert artifact_type == "executive_summary"


@pytest.mark.asyncio
async def test_s7_artifact_contains_markdown():
    """The Artifact content_md matches the markdown field from the LLM response."""
    from app.stages import s7_summary

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(_VALID_S7_FULL_RESPONSE))
    store = _make_store()

    with patch("app.stages.s7_summary.TemplateService") as mock_ts:
        mock_ts.return_value.load_template.return_value = "template"
        await s7_summary.run(
            S7Input(mode="decide", s5_output=_make_s5_output(), s6b_output=_make_s6b_output()),
            _make_context(),
            llm,
            store,
        )

    call_kwargs = store.save_artifact.call_args
    content_md = call_kwargs.kwargs.get("content_md") or call_kwargs.args[2]
    assert content_md == _VALID_S7_FULL_RESPONSE["markdown"]


# ---------------------------------------------------------------------------
# Prior stage loading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s7_loads_prior_stages_from_store():
    """S7 calls get_stage_output for s1, s2, s3, s4 to build the prompt."""
    from app.stages import s7_summary

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(_VALID_S7_FULL_RESPONSE))
    store = _make_store(has_prior_stages=True)

    with patch("app.stages.s7_summary.TemplateService") as mock_ts:
        mock_ts.return_value.load_template.return_value = "template"
        await s7_summary.run(
            S7Input(mode="decide", s5_output=_make_s5_output(), s6b_output=_make_s6b_output()),
            _make_context(),
            llm,
            store,
        )

    called_stages = {call.args[1] for call in store.get_stage_output.call_args_list}
    assert {"s1", "s2", "s3", "s4"}.issubset(called_stages)


@pytest.mark.asyncio
async def test_s7_handles_missing_prior_stages_gracefully():
    """S7 runs without error when prior stage outputs are absent (returns None)."""
    from app.stages import s7_summary

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(_VALID_S7_PARTIAL_RESPONSE))
    store = _make_store(has_prior_stages=False)  # all get_stage_output return None

    with patch("app.stages.s7_summary.TemplateService") as mock_ts:
        mock_ts.return_value.load_template.return_value = "template"
        out = await s7_summary.run(
            S7Input(mode="brief"),
            _make_context(),
            llm,
            store,
        )

    assert isinstance(out, S7Output)


# ---------------------------------------------------------------------------
# JSON parsing — markdown fence unwrapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s7_unwraps_markdown_fenced_json():
    """LLM response wrapped in ```json ... ``` fences is parsed correctly."""
    from app.stages import s7_summary

    fenced = "```json\n" + json.dumps(_VALID_S7_FULL_RESPONSE) + "\n```"
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=fenced)
    store = _make_store()

    with patch("app.stages.s7_summary.TemplateService") as mock_ts:
        mock_ts.return_value.load_template.return_value = "template"
        out = await s7_summary.run(
            S7Input(mode="decide", s5_output=_make_s5_output(), s6b_output=_make_s6b_output()),
            _make_context(),
            llm,
            store,
        )

    assert out.output.what_we_saw == _VALID_S7_FULL_RESPONSE["what_we_saw"]


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s7_missing_required_field_raises():
    """LLM response missing a required field raises an error."""
    from app.stages import s7_summary

    incomplete = {k: v for k, v in _VALID_S7_FULL_RESPONSE.items() if k != "what_we_saw"}
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(incomplete))
    store = _make_store()

    with patch("app.stages.s7_summary.TemplateService") as mock_ts:
        mock_ts.return_value.load_template.return_value = "template"
        with pytest.raises(Exception):  # pydantic ValidationError
            await s7_summary.run(
                S7Input(mode="decide", s5_output=_make_s5_output(), s6b_output=_make_s6b_output()),
                _make_context(),
                llm,
                store,
            )
