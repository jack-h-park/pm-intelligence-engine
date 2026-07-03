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
        product_id="example-security-product",
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
        {"statement": "KPE Ultra customers need unified APM policy", "severity": "Adjusting", "reason": "If false, scope narrows but value proposition survives"},
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
    from app.stages.s5_prioritization import _DEFAULT_WEIGHTS as _WEIGHTS
    scores = {"explorer": 4, "strategist": 5, "builder": 4, "skeptic": 4}
    expected = round(4 * 0.35 + 5 * 0.30 + 4 * 0.20 + 4 * 0.15, 2)
    computed = round(sum(scores[p] * w for p, w in _WEIGHTS.items()), 2)
    assert computed == expected
    assert computed == pytest.approx(4.30, abs=0.01)


def test_composite_kill_range():
    """Low scores produce composite ≤ 1.5 → kill routing."""
    from app.stages.s5_prioritization import _DEFAULT_WEIGHTS as _WEIGHTS
    scores = {"explorer": 1, "strategist": 1, "builder": 2, "skeptic": 1}
    composite = round(sum(scores[p] * w for p, w in _WEIGHTS.items()), 2)
    assert composite <= 1.5


# ---------------------------------------------------------------------------
# Routing logic
# ---------------------------------------------------------------------------


def test_routing_prd_high_composite_no_blocking():
    from app.stages.s5_prioritization import _compute_routing
    assert _compute_routing(4.30, confidence=4, blocking=[]) == "prd"


def test_routing_prd_composite_at_threshold():
    from app.stages.s5_prioritization import _compute_routing
    assert _compute_routing(3.5, confidence=4, blocking=[]) == "prd"


def test_routing_poc_composite_below_threshold():
    from app.stages.s5_prioritization import _compute_routing
    assert _compute_routing(3.0, confidence=4, blocking=[]) == "poc"


def test_routing_blocking_with_decent_composite_routes_poc():
    """A Blocking assumption on a decent-value opportunity is an unresolved
    question → poc (validate), not kill. This is the run-9b49fc8c pattern that
    the old `blocking → kill` rule wrongly killed."""
    from app.stages.s5_prioritization import _compute_routing
    blocking = [Assumption(statement="Partner needed", severity="Blocking", reason="No partner = no product")]
    assert _compute_routing(3.0, confidence=3, blocking=blocking) == "poc"


def test_routing_kill_low_composite():
    from app.stages.s5_prioritization import _compute_routing
    assert _compute_routing(1.35, confidence=3, blocking=[]) == "kill"


def test_routing_kill_low_composite_even_with_blocking():
    """The kill floor is checked before blocking: a low-value opportunity still
    kills even if it also has blockers (R06 pattern: composite 1.35 + 3 Blocking)."""
    from app.stages.s5_prioritization import _compute_routing
    blocking = [Assumption(statement="Out of scope", severity="Blocking", reason="MTD not our product")]
    assert _compute_routing(1.35, confidence=3, blocking=blocking) == "kill"


# ---------------------------------------------------------------------------
# Two-axis hybrid routing boundary matrix (US-29, revised: blocking → poc)
#
# Rule (04-scoring.md "Routing Decision — Two-Axis Hybrid Rule"):
#   1. composite <= 1.5                        -> kill   (low value; value floor)
#   2. blocking                                -> poc    (unresolved → validate)
#   3. composite >= 3.5 AND confidence >= 4    -> prd
#   4. composite >= 3.5 AND confidence < 4     -> poc
#   5. otherwise                               -> poc

# ---------------------------------------------------------------------------

def test_hybrid_strong_and_validated_routes_prd():
    from app.stages.s5_prioritization import _compute_routing
    assert _compute_routing(4.30, confidence=4, blocking=[]) == "prd"


def test_hybrid_strong_but_unvalidated_routes_poc():
    # Case A from 04-scoring.md: 5/5/4/2 -> composite 4.35, confidence 2
    from app.stages.s5_prioritization import _compute_routing
    assert _compute_routing(4.35, confidence=2, blocking=[]) == "poc"


def test_hybrid_confidence_gate_boundary_3_vs_4():
    from app.stages.s5_prioritization import _compute_routing
    assert _compute_routing(4.50, confidence=3, blocking=[]) == "poc"  # R05 pattern
    assert _compute_routing(4.50, confidence=4, blocking=[]) == "prd"


def test_hybrid_prd_threshold_boundary():
    from app.stages.s5_prioritization import _compute_routing
    assert _compute_routing(3.5, confidence=4, blocking=[]) == "prd"   # at threshold
    assert _compute_routing(3.4, confidence=5, blocking=[]) == "poc"   # just below


def test_hybrid_modest_but_certain_routes_poc():
    # Case B from 04-scoring.md: 3/3/3/4 -> composite 3.15, confidence 4
    from app.stages.s5_prioritization import _compute_routing
    assert _compute_routing(3.15, confidence=4, blocking=[]) == "poc"


def test_hybrid_kill_threshold_boundary():
    from app.stages.s5_prioritization import _compute_routing
    assert _compute_routing(1.5, confidence=5, blocking=[]) == "kill"  # at threshold
    assert _compute_routing(1.6, confidence=1, blocking=[]) == "poc"   # just above


def test_thresholds_default_when_no_scoring_yaml(tmp_path):
    from app.stages.s5_prioritization import _DEFAULT_THRESHOLDS, _load_thresholds
    with patch("config.settings.DECISION_CONTEXT_ROOT", str(tmp_path)):
        assert _load_thresholds("nonexistent-product") == _DEFAULT_THRESHOLDS


def test_thresholds_overridable_per_product(tmp_path):
    from app.stages.s5_prioritization import _compute_routing, _load_thresholds
    product_dir = tmp_path / "products" / "test-product"
    product_dir.mkdir(parents=True)
    (product_dir / "scoring.yaml").write_text(
        "impact: 0.35\nstrategic_fit: 0.30\nfeasibility: 0.20\nconfidence: 0.15\n"
        "kill_threshold: 2.0\nprd_threshold: 4.0\nconfidence_gate: 3\n"
    )
    with patch("config.settings.DECISION_CONTEXT_ROOT", str(tmp_path)):
        t = _load_thresholds("test-product")
    assert t == {"kill_threshold": 2.0, "prd_threshold": 4.0, "confidence_gate": 3.0}
    # Custom thresholds change routing outcomes
    assert _compute_routing(1.8, confidence=5, blocking=[], thresholds=t) == "kill"
    assert _compute_routing(4.0, confidence=3, blocking=[], thresholds=t) == "prd"


def test_thresholds_partial_yaml_falls_back_per_key(tmp_path):
    from app.stages.s5_prioritization import _load_thresholds
    product_dir = tmp_path / "products" / "test-product"
    product_dir.mkdir(parents=True)
    (product_dir / "scoring.yaml").write_text("prd_threshold: 4.0\n")
    with patch("config.settings.DECISION_CONTEXT_ROOT", str(tmp_path)):
        t = _load_thresholds("test-product")
    assert t["prd_threshold"] == 4.0
    assert t["kill_threshold"] == 1.5
    assert t["confidence_gate"] == 4


def test_hybrid_blocking_high_composite_routes_poc():
    """Blocking no longer overrides the axes into kill — a high-value opportunity
    with an unresolved blocker routes to poc (validate before PRD commit)."""
    from app.stages.s5_prioritization import _compute_routing
    blocking = [Assumption(statement="Feasibility unknown", severity="Blocking", reason="Can we build it?")]
    assert _compute_routing(4.5, confidence=5, blocking=blocking) == "poc"


def test_legacy_informing_severity_coerced_to_adjusting():
    """Old stored runs (and stray LLM output) with 'Informing' read as 'Adjusting'."""
    a = Assumption(statement="x", severity="Informing", reason="legacy")
    assert a.severity == "Adjusting"
    parsed = Assumption.model_validate({"statement": "x", "severity": "Informing", "reason": "r"})
    assert parsed.severity == "Adjusting"


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
async def test_s5_governing_heuristics_flow_through():
    """governing_heuristics from the LLM appear in output and decision memo (US-32)."""
    from app.stages import s5_prioritization

    resp = json.dumps({
        "assumptions": [
            {"statement": "Admins want unified enforcement", "severity": "Adjusting",
             "reason": "Scope narrows if false"},
        ],
        "rationale": "Strong fit, no blocking assumptions.",
        "governing_heuristics": ["#7", "#14"],
    })
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=resp)
    store = _make_store()

    with patch("app.stages.s5_prioritization.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template"
        output = await s5_prioritization.run(
            S5Input(s4_output=_make_s4_output(explorer=4, strategist=5, builder=4, skeptic=4)),
            _make_context(),
            llm,
            store,
        )

    assert output.output.governing_heuristics == ["#7", "#14"]
    # Rendered into the decision memo artifact
    memo_call = [c for c in store.save_artifact.call_args_list
                 if c.kwargs.get("artifact_type") == "decision_memo"]
    assert memo_call and "#7, #14" in memo_call[0].kwargs["content_md"]


@pytest.mark.asyncio
async def test_s5_governing_heuristics_default_empty():
    """Backward compatible: response without governing_heuristics yields []."""
    from app.stages import s5_prioritization

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=_LLM_RESPONSE_NO_BLOCKING)
    store = _make_store()
    with patch("app.stages.s5_prioritization.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template"
        output = await s5_prioritization.run(
            S5Input(s4_output=_make_s4_output()),
            _make_context(),
            llm,
            store,
        )
    assert output.output.governing_heuristics == []


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


# ---------------------------------------------------------------------------
# Blocking-assumption verifier (US-42)
# ---------------------------------------------------------------------------


def _classification(statement: str, severity: str = "Blocking") -> str:
    return json.dumps({
        "assumptions": [{"statement": statement, "severity": severity, "reason": "claimed"}],
        "rationale": "x",
        "governing_heuristics": [],
    })


@pytest.mark.asyncio
async def test_verifier_downgrades_overeager_blocking(capsys):
    """Verifier finds an alternative path → Blocking downgraded → kill flips to prd."""
    from app.stages import s5_prioritization

    stmt = "No native admin-enforcement API exists"
    verifier = json.dumps({"verdicts": [
        {"statement": stmt, "keep_blocking": False, "reason": "Knox provides an alternative path"}
    ]})
    llm = AsyncMock()
    llm.complete = AsyncMock(side_effect=[_classification(stmt), verifier])
    store = _make_store()

    with patch("config.settings.BLOCKING_VERIFIER_ENABLED", True), \
         patch("app.stages.s5_prioritization.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template"
        out = await s5_prioritization.run(
            S5Input(s4_output=_make_s4_output(explorer=5, strategist=5, builder=4, skeptic=4)),
            _make_context(), llm, store,
        )

    assert llm.complete.call_count == 2  # classification + verifier
    assert out.output.blocking_count == 0
    assert out.output.assumptions[0].severity == "Adjusting"
    assert out.output.routing == "prd"  # composite 4.65, confidence 4, no blocking
    events = [json.loads(l) for l in capsys.readouterr().out.strip().splitlines() if l.strip().startswith("{")]
    assert any(e["action"] == "blocking_downgraded" for e in events)


@pytest.mark.asyncio
async def test_verifier_keeps_genuine_blocking():
    """Verifier confirms no alternative path → Blocking kept → poc.

    The kept Blocking no longer forces kill (composite 3.65 > kill floor); an
    unresolved genuine blocker routes to poc to validate before committing."""
    from app.stages import s5_prioritization

    stmt = "DISA certification is required and unobtainable in time"
    verifier = json.dumps({"verdicts": [
        {"statement": stmt, "keep_blocking": True, "reason": "no alternative path exists"}
    ]})
    llm = AsyncMock()
    llm.complete = AsyncMock(side_effect=[_classification(stmt), verifier])
    store = _make_store()

    with patch("config.settings.BLOCKING_VERIFIER_ENABLED", True), \
         patch("app.stages.s5_prioritization.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template"
        out = await s5_prioritization.run(
            S5Input(s4_output=_make_s4_output(explorer=4, strategist=4, builder=3, skeptic=3)),
            _make_context(), llm, store,
        )

    assert llm.complete.call_count == 2
    assert out.output.blocking_count == 1
    assert out.output.routing == "poc"


@pytest.mark.asyncio
async def test_verifier_skipped_when_disabled():
    from app.stages import s5_prioritization

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=_classification("Some blocker"))
    store = _make_store()

    with patch("config.settings.BLOCKING_VERIFIER_ENABLED", False), \
         patch("app.stages.s5_prioritization.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template"
        out = await s5_prioritization.run(
            S5Input(s4_output=_make_s4_output(explorer=2, strategist=2, builder=2, skeptic=2)),
            _make_context(), llm, store,
        )

    assert llm.complete.call_count == 1  # verifier not called
    assert out.output.blocking_count == 1


@pytest.mark.asyncio
async def test_verifier_skipped_when_no_blocking():
    from app.stages import s5_prioritization

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=_LLM_RESPONSE_NO_BLOCKING)  # all Adjusting
    store = _make_store()

    with patch("config.settings.BLOCKING_VERIFIER_ENABLED", True), \
         patch("app.stages.s5_prioritization.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template"
        await s5_prioritization.run(
            S5Input(s4_output=_make_s4_output()), _make_context(), llm, store,
        )

    assert llm.complete.call_count == 1  # no blocking → verifier not called


# ---------------------------------------------------------------------------
# Value Horizon — closing-window flag (US-41)
# ---------------------------------------------------------------------------


def _store_with_value_horizon(vh: str) -> MagicMock:
    store = _make_store()
    store.get_stage_output = MagicMock(
        return_value={"output_json": json.dumps({"output": {"value_horizon": vh}})}
    )
    return store


async def _run_s5(llm_response: str, store, **s4_scores):
    from app.stages import s5_prioritization
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=llm_response)
    with patch("app.stages.s5_prioritization.TemplateService") as MockTS:
        MockTS.return_value.load_template.return_value = "template"
        return await s5_prioritization.run(
            S5Input(s4_output=_make_s4_output(**s4_scores)),
            _make_context(), llm, store,
        )


@pytest.mark.asyncio
async def test_closing_window_flagged_when_transient_high_impact_no_blocking():
    out = await _run_s5(_LLM_RESPONSE_NO_BLOCKING, _store_with_value_horizon("transient"),
                        explorer=5, strategist=4, builder=4, skeptic=4)
    assert out.output.closing_window is True
    assert out.output.blocking_count == 0


@pytest.mark.asyncio
async def test_closing_window_not_flagged_when_durable():
    out = await _run_s5(_LLM_RESPONSE_NO_BLOCKING, _store_with_value_horizon("durable"),
                        explorer=5, strategist=4, builder=4, skeptic=4)
    assert out.output.closing_window is False


@pytest.mark.asyncio
async def test_closing_window_not_flagged_when_blocking_present():
    # transient + high impact, but a Blocking assumption exists → no closing window
    out = await _run_s5(_LLM_RESPONSE_BLOCKING, _store_with_value_horizon("transient"),
                        explorer=5, strategist=4, builder=4, skeptic=4)
    assert out.output.blocking_count >= 1
    assert out.output.closing_window is False


@pytest.mark.asyncio
async def test_closing_window_not_flagged_when_impact_low():
    # transient + no blocking, but Impact (explorer) below threshold (4)
    out = await _run_s5(_LLM_RESPONSE_NO_BLOCKING, _store_with_value_horizon("transient"),
                        explorer=3, strategist=3, builder=3, skeptic=4)
    assert out.output.closing_window is False


@pytest.mark.asyncio
async def test_value_horizon_defaults_durable_when_s3_absent():
    from app.stages import s5_prioritization
    store = _make_store()
    store.get_stage_output = MagicMock(return_value=None)  # no S3 output stored
    out = await _run_s5(_LLM_RESPONSE_NO_BLOCKING, store, explorer=5, strategist=5, builder=5, skeptic=5)
    assert out.output.closing_window is False
