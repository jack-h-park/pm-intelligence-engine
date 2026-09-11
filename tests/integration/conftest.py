"""Shared integration-test fixtures.

Server-side auth (``require_auth``) is now applied to every router. The
business-logic integration tests are not concerned with auth, so this autouse
fixture bypasses it by default. Tests that exercise auth itself
(``test_auth.py``) remove this override and assert real enforcement.
"""

from typing import Any

import pytest

from app.api.deps import require_auth
from app.api.main import app


@pytest.fixture(autouse=True)
def bypass_auth():
    app.dependency_overrides[require_auth] = lambda: None
    yield
    app.dependency_overrides.pop(require_auth, None)


# --- Legacy-status test helpers (US-55 step 7d-1) ---------------------------
# The store dropped the `status`/`current_stage` write path in favour of the
# canonical (lifecycle, position, outcome) columns. Existing tests still describe
# run state with the old status names; these two helpers translate between the two
# so the tests read the same while driving the new store API.

# Gate position for each paused status.
_GATE_POSITION = {
    "waiting_direction": "s2",
    "waiting_approval": "s4",
    "waiting_routing_review": "s5",
}
# Terminal status -> (outcome, position, reason/ended_by).
_TERMINAL = {
    "completed": "completed",
    "killed": "stopped",
    "failed": "failed",
}


def seed_run_state(store, run_id: str, status: str, mode: str | None = None) -> None:
    """Put a run into the state a legacy ``status`` name described, via the
    canonical store writers (advance/pause/finish)."""
    if mode is not None:
        store.update_run(run_id, mode=mode)
    if status in ("pending", "running"):
        store.advance(run_id, "s1" if status == "pending" else "s3")
    elif status in _GATE_POSITION:
        store.pause(run_id, _GATE_POSITION[status])
    elif status in _TERMINAL:
        outcome = _TERMINAL[status]
        position = "s7" if status == "completed" else None
        store.finish(run_id, outcome, position=position)
    else:  # pragma: no cover - guard against typos in tests
        raise ValueError(f"unknown legacy status {status!r}")


def run_status(run: dict[str, Any]) -> str:
    """Reconstruct the legacy ``status`` string from a run's canonical columns,
    for assertions that still speak the old vocabulary."""
    lifecycle, position, outcome = (
        run.get("lifecycle"), run.get("position"), run.get("outcome"),
    )
    if lifecycle == "done":
        assert isinstance(outcome, str)
        return {"completed": "completed", "stopped": "killed", "failed": "failed"}[outcome]
    if lifecycle == "paused":
        assert isinstance(position, str)
        return {v: k for k, v in _GATE_POSITION.items()}[position]
    return "running"
