"""Unit tests for the (position, lifecycle) projection (app/run_view.py).

Locks the status/mode/current_stage → lifecycle/position/target/outcome/reason
mapping that the redesign's read model derives, so the eventual physical column
flip can be verified against exactly this table.
"""

import pytest

from app import run_view


@pytest.mark.parametrize("status,expected", [
    ("pending", "running"),
    ("running", "running"),
    ("waiting_direction", "paused"),
    ("waiting_approval", "paused"),
    ("waiting_routing_review", "paused"),
    ("completed", "done"),
    ("killed", "done"),
    ("failed", "done"),
])
def test_lifecycle_mapping(status, expected):
    assert run_view.lifecycle(status) == expected


@pytest.mark.parametrize("status,expected", [
    ("running", None),
    ("waiting_approval", None),
    ("completed", "completed"),
    ("killed", "stopped"),
    ("failed", "failed"),
])
def test_outcome_mapping(status, expected):
    assert run_view.outcome(status) == expected


@pytest.mark.parametrize("mode,expected_target", [
    ("archive", "s1"), ("note", "s2"), ("structure", "s3"),
    ("evaluate", "s4"), ("decide", "s7"), (None, None),
])
def test_target_from_depth(mode, expected_target):
    assert run_view.target({"mode": mode}) == expected_target


def test_position_prefers_live_current_stage():
    assert run_view.position({"status": "waiting_approval", "current_stage": "s4"}) == "s4"


def test_position_inferred_for_completed_from_target():
    # current_stage cleared on finalize; a completed run reached its target.
    assert run_view.position({"status": "completed", "current_stage": None, "mode": "structure"}) == "s3"


def test_position_inferred_for_failed_from_failed_stage():
    assert run_view.position(
        {"status": "failed", "current_stage": None, "failed_stage": "s3"}
    ) == "s3"


def test_reason_is_error_on_failure():
    assert run_view.reason({"status": "failed", "error": "boom"}) == "boom"


def test_reason_is_ended_by_on_kill():
    assert run_view.reason({"status": "killed", "ended_by": "voided"}) == "voided"


def test_reason_none_for_completed_and_live():
    assert run_view.reason({"status": "completed", "ended_by": "decided"}) is None
    assert run_view.reason({"status": "running"}) is None


def test_project_full_shape():
    run = {"status": "waiting_routing_review", "current_stage": "s5", "mode": "decide"}
    # `target` is intentionally NOT in the projected shape (= depth 1:1, US-55).
    assert run_view.project(run) == {
        "lifecycle": "paused", "position": "s5",
        "outcome": None, "reason": None,
    }


def test_project_completed_decide():
    run = {"status": "completed", "current_stage": None, "mode": "decide", "ended_by": "decided"}
    p = run_view.project(run)
    assert p["lifecycle"] == "done"
    assert p["outcome"] == "completed"
    assert p["position"] == "s7"
    assert p["reason"] is None


def test_run_response_derives_when_stored_columns_are_null():
    """A pre-dual-write row carries NULL lifecycle/position columns; the API must
    still derive them (regression guard for the step-6 serializer change)."""
    from app.api.runs import RunResponse

    row = {
        "run_id": "r0", "product_id": "p", "signal_id": "s",
        "status": "waiting_approval", "current_stage": "s4", "mode": "decide",
        "recommendation_json": None, "routing": None, "composite_score": None,
        "created_at": "2026-07-03T00:00:00", "completed_at": None,
        # Legacy row: stored projection columns are NULL.
        "lifecycle": None, "position": None, "outcome": None, "reason": None,
    }
    resp = RunResponse(**row)
    assert resp.lifecycle == "paused"   # derived, not the stored NULL
    assert resp.position == "s4"
    assert not hasattr(resp, "target")  # target is not surfaced (US-55; = depth 1:1)


def test_run_response_prefers_stored_columns_when_present():
    """A dual-written row's real columns win over re-derivation."""
    from app.api.runs import RunResponse

    row = {
        "run_id": "r0b", "product_id": "p", "signal_id": "s",
        "status": "completed", "current_stage": None, "mode": "decide",
        "recommendation_json": None, "routing": None, "composite_score": None,
        "created_at": "2026-07-03T00:00:00", "completed_at": "2026-07-03T01:00:00",
        "lifecycle": "done", "position": "s7", "outcome": "completed", "reason": None,
    }
    resp = RunResponse(**row)
    assert resp.lifecycle == "done"
    assert resp.position == "s7"
    assert resp.outcome == "completed"


def test_run_response_surfaces_projection():
    """The API model derives the new fields from the stored row (US-55 step 5)."""
    from app.api.runs import RunResponse

    row = {
        "run_id": "r1", "product_id": "p", "signal_id": "s",
        "status": "waiting_approval", "current_stage": "s4", "mode": "decide",
        "recommendation_json": None, "routing": None, "composite_score": None,
        "created_at": "2026-07-03T00:00:00", "completed_at": None,
    }
    resp = RunResponse(**row)
    assert resp.lifecycle == "paused"
    assert resp.position == "s4"
    assert resp.outcome is None
    # depth is still surfaced too (legacy field kept during the transition).
    assert resp.depth == "decide"


# --- step 7a: status ↔ (lifecycle, position, outcome) round-trip -------------
# Locks the bijection that step 7b (flip authority: store the state, derive
# status) will rely on. status → project → status_of must be identity for every
# reachable RunStatus, so the eventual flip cannot silently change any state.

@pytest.mark.parametrize("status,row", [
    ("running", {"status": "running", "current_stage": "s3"}),
    ("waiting_direction", {"status": "waiting_direction", "current_stage": "s2"}),
    ("waiting_approval", {"status": "waiting_approval", "current_stage": "s4"}),
    ("waiting_routing_review", {"status": "waiting_routing_review", "current_stage": "s5"}),
    ("completed", {"status": "completed", "current_stage": None, "mode": "decide"}),
    ("killed", {"status": "killed", "current_stage": None, "ended_by": "kill_confirmed"}),
    ("failed", {"status": "failed", "current_stage": None, "failed_stage": "s3"}),
])
def test_status_roundtrip_is_identity(status, row):
    p = run_view.project(row)
    assert run_view.status_of(p["lifecycle"], p["position"], p["outcome"]) == status


def test_pending_collapses_to_running():
    # pending has no distinct state slot — it round-trips to running (documented).
    p = run_view.project({"status": "pending", "current_stage": None})
    assert run_view.status_of(p["lifecycle"], p["position"], p["outcome"]) == "running"


def test_status_of_paused_without_gate_raises():
    # A paused run must sit at a gate position (s2/s4/s5); anything else is unreachable.
    with pytest.raises(ValueError):
        run_view.status_of("paused", "s3", None)
