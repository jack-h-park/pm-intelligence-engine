"""Unit tests for Stage 5 — Prioritization and routing."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.stages import (
    Assumption,
    PersonaOutput,
    RunContext,
    S4OutputData,
    S4RubricResult,
    S5Input,
)


def _make_context() -> RunContext:
    return RunContext(
        run_id="test-run-s5",
        product_id="samsung-knox-lockdown-mode",
        pm_identity="PM identity",
        company_context="Company context",
        product_context="Strategy Pillar: **Attack Surface Reduction**.",
    )


def _make_store() -> MagicMock:
    store = MagicMock()
    store.save_stage_output = MagicMock(return_value="output-id")
    return store


def _make_s4_output(
    explorer: int = 4,
    strategist: int = 5,
    builder: int = 4,
    skeptic: int = 4,
) -> S4OutputData:
    rubric = S4RubricResult(
        total_score=11,
        score_grounding=3,
        skeptic_quality=3,
        open_question_quality=2,
        persona_independence=3,
        passed=True,
        issues=[],
    )
    return S4OutputData(
        personas=[
            PersonaOutput(persona="explorer", dimension="Impact", score=explorer, key_argument="Strong impact.", open_question="Customer interview needed."),
            PersonaOutput(persona="strategist", dimension="Strategic Fit", score=strategist, key_argument="Strong fit.", open_question="Legal review needed."),
            PersonaOutput(persona="builder", dimension="Feasibility", score=builder, key_argument="Feasible.", open_question="Engineering spike needed."),
            PersonaOutput(persona="skeptic", dimension="Confidence", score=skeptic, key_argument="No counter-argument strong enough to kill.", open_question="Would a customer survey reveal redundancy?"),
        ],
        rubric=rubric,
    )


_LLM_RESPONSE_NO_BLOCKING = json.dumps({
    "assumptions": [
        {"statement": "KPE Ultra customers need unified APM policy", "severity": "Informing", "reason": "If false, scope narrows but value proposition survives"},
    ],
    "rationale": "High composite score with no Blocking assumptions. PRD track is appropriate.",
})

_LLM_RESPONSE_BLOCKING = json.dumps({
    "assumptions": [
        {"statement": "MTD integration is within KPE Ultra scope", "severity": "Blocking", "reason": "If false, entire opportunity is out-of-scope for this team"},
        {"statement": "Partner agreement is achievable", "severity": "Blocking", "reason": "If false, no product can be built"},
        {"statement": "Government mandates our specific MTD", "severity": "Blocking", "reason": "If false, customer will use a competitor"},
    ],
    "rationale": "Three Blocking assumptions make this opportunity too risky to proceed.",
})


# ---------------------------------------------------------------------------
# Composite score calculation
# ---------------------------------------------------------------------------


def test_composite_formula():
    """Composite = Impact×0.35 + StrategicFit×0.30 + Feasibility×0.20 + Confidence×0.15."""
    from app.stages.s5_prioritization import _WEIGHTS
    scores = {"explorer": 4, "strategist": 5, "builder": 4, "skeptic": 4}
    expected = round(4 * 0.35 + 5 * 0.30 + 4 * 0.20 + 4 * 0.15, 2)
    computed = round(sum(scores[p] * w for p, w in _WEIGHTS.items()), 2)
    assert computed == expected
    assert computed == pytest.approx(4.30, abs=0.01)


def test_composite_kill_range():
    """Low scores produce composite ≤ 1.5 → kill routing."""
    from app.stages.s5_prioritization import _WEIGHTS
    scores = {"explorer": 1, "strategist": 1, "builder": 2, "skeptic": 1}
    composite = round(sum(scores[p] * w for p, w in _WEIGHTS.items()), 2)
    assert composite <= 1.5


# ---------------------------------------------------------------------------
# Routing logic
# ---------------------------------------------------------------------------


def test_routing_prd_high_composite_no_blocking():
    from app.stages.s5_prioritization import _compute_routing
    assert _compute_routing(4.30, blocking=[]) == "prd"


def test_routing_prd_composite_at_threshold():
    from app.stages.s5_prioritization import _compute_routing
    assert _compute_routing(3.5, blocking=[]) == "prd"


def test_routing_poc_composite_below_threshold():
    from app.stages.s5_prioritization import _compute_routing
    assert _compute_routing(3.0, blocking=[]) == "poc"


def test_routing_kill_with_blocking_assumptions():
    from app.stages.s5_prioritization import _compute_routing
    blocking = [Assumption(statement="Partner needed", severity="Blocking", reason="No partner = no product")]
    assert _compute_routing(3.0, blocking=blocking) == "kill"


def test_routing_kill_low_composite():
    from app.stages.s5_prioritization import _compute_routing
    assert _compute_routing(1.35, blocking=[]) == "kill"


def test_routing_kill_blocking_overrides_high_composite():
    from app.stages.s5_prioritization import _compute_routing
    blocking = [Assumption(statement="Fatal assumption", severity="Blocking", reason="Fatal")]
    assert _compute_routing(4.5, blocking=blocking) == "kill"


# ---------------------------------------------------------------------------
# S5 stage integration (mocked LLM)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s5_prd_routing():
    from app.stages import s5_prioritization

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=_LLM_RESPONSE_NO_BLOCKING)
    store = _make_store()

    with patch("app.stages.s5_prioritization.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template"
        output = await s5_prioritization.run(
            S5Input(s4_output=_make_s4_output(explorer=4, strategist=5, builder=4, skeptic=4)),
            _make_context(),
            llm,
            store,
        )

    assert output.output.routing == "prd"
    assert output.output.composite_score == pytest.approx(4.30, abs=0.01)
    assert output.output.blocking_count == 0


@pytest.mark.asyncio
async def test_s5_kill_routing_with_blocking():
    from app.stages import s5_prioritization

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=_LLM_RESPONSE_BLOCKING)
    store = _make_store()

    with patch("app.stages.s5_prioritization.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template"
        output = await s5_prioritization.run(
            S5Input(s4_output=_make_s4_output(explorer=1, strategist=1, builder=2, skeptic=1)),
            _make_context(),
            llm,
            store,
        )

    assert output.output.routing == "kill"
    assert output.output.blocking_count == 3


@pytest.mark.asyncio
async def test_s5_poc_routing_low_composite():
    """composite < 3.5 with no blocking assumptions → poc."""
    from app.stages import s5_prioritization

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=_LLM_RESPONSE_NO_BLOCKING)
    store = _make_store()

    # explorer=3, strategist=3, builder=3, skeptic=3 → composite = 3.00 → poc
    with patch("app.stages.s5_prioritization.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template"
        output = await s5_prioritization.run(
            S5Input(s4_output=_make_s4_output(explorer=3, strategist=3, builder=3, skeptic=3)),
            _make_context(),
            llm,
            store,
        )

    assert output.output.routing == "poc"
    assert output.output.composite_score == pytest.approx(3.00, abs=0.01)


@pytest.mark.asyncio
async def test_s5_saves_to_store():
    from app.stages import s5_prioritization

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=_LLM_RESPONSE_NO_BLOCKING)
    store = _make_store()

    with patch("app.stages.s5_prioritization.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template"
        await s5_prioritization.run(
            S5Input(s4_output=_make_s4_output()),
            _make_context(),
            llm,
            store,
        )

    store.save_stage_output.assert_called_once()
    call_kwargs = store.save_stage_output.call_args
    assert call_kwargs.kwargs.get("stage") == "s5" or call_kwargs.args[1] == "s5"
