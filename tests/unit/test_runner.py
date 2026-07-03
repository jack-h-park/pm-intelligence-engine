"""Unit tests for the pipeline flow planner (app/runner.py).

These lock the entire stage-flow — which stages run for each depth, where the
gates fire, and how the S6 branch resolves — in one place, replacing the implicit
coverage that was spread across the per-gate execution functions.
"""

import pytest

from app import runner
from app.runner import AdvancePlan, plan_advance, target_for_depth


# ---------------------------------------------------------------------------
# depth → target position
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("depth,target", [
    ("archive", "s1"),
    ("note", "s2"),
    ("structure", "s3"),
    ("evaluate", "s4"),
    ("decide", "s7"),
])
def test_target_for_depth(depth, target):
    assert target_for_depth(depth) == target


# ---------------------------------------------------------------------------
# Shallow depths: run to the stop position, no gate
# ---------------------------------------------------------------------------


def test_archive_completes_immediately():
    # S1+S2 already ran before Gate 1; archive (target S1) is already past.
    assert plan_advance("s2", target_for_depth("archive")) == AdvancePlan(
        run=(), then="complete", at="s2"
    )


def test_note_stops_at_s2_no_s7():
    # Clean model: note = stop at S2, the insight memo is the artifact (no S7 jump).
    assert plan_advance("s2", target_for_depth("note")) == AdvancePlan(
        run=(), then="complete", at="s2"
    )


def test_structure_runs_s3_only():
    assert plan_advance("s2", target_for_depth("structure")) == AdvancePlan(
        run=("s3",), then="complete", at="s3"
    )


def test_evaluate_runs_s3_s4_no_gate():
    # evaluate stops AT S4 — the S4 pause does not fire when it is the target.
    assert plan_advance("s2", target_for_depth("evaluate")) == AdvancePlan(
        run=("s3", "s4"), then="complete", at="s4"
    )


# ---------------------------------------------------------------------------
# decide: three segments, pausing at Gate 2 (s4) then Gate 3 (s5)
# ---------------------------------------------------------------------------


def test_decide_segment1_pauses_at_gate2():
    assert plan_advance("s2", target_for_depth("decide")) == AdvancePlan(
        run=("s3", "s4"), then="pause", at="s4"
    )


def test_decide_segment2_pauses_at_gate3():
    assert plan_advance("s4", target_for_depth("decide")) == AdvancePlan(
        run=("s5",), then="pause", at="s5"
    )


def test_decide_segment3_poc_runs_s6a_s7():
    assert plan_advance("s5", target_for_depth("decide"), routing="poc") == AdvancePlan(
        run=("s6a", "s7"), then="complete", at="s7"
    )


def test_decide_segment3_prd_runs_s6b_s7():
    assert plan_advance("s5", target_for_depth("decide"), routing="prd") == AdvancePlan(
        run=("s6b", "s7"), then="complete", at="s7"
    )


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_cannot_advance_past_s5_without_routing():
    with pytest.raises(ValueError, match="routing"):
        plan_advance("s5", "s7", routing=None)


def test_already_at_target_completes():
    assert plan_advance("s3", "s3") == AdvancePlan(run=(), then="complete", at="s3")


def test_full_decide_journey_reconstructs_every_stage():
    """Chaining the segments must run exactly s3,s4,s5,s6a,s7 with two pauses."""
    ran: list[str] = []
    pauses: list[str] = []
    current, target, routing = "s2", "s7", None

    for _ in range(5):  # bounded; the journey is 3 segments
        plan = plan_advance(current, target, routing)
        ran.extend(plan.run)
        if plan.then == "complete":
            break
        pauses.append(plan.at)
        current = plan.at
        if current == "s5":  # Gate 3 resolves routing
            routing = "poc"

    assert ran == ["s3", "s4", "s5", "s6a", "s7"]
    assert pauses == ["s4", "s5"]  # Gate 2, Gate 3
