"""Canonical (position, lifecycle) projection of a run — US-55 step 5.

The redesign's target vocabulary is ``lifecycle`` / ``position`` / ``target`` /
``outcome`` / ``reason`` (docs/WORKFLOW_MODEL_REDESIGN.md §2). This module derives
it from the current storage fields (``status`` / ``mode`` / ``current_stage`` /
``ended_by`` / ``error``).

**Why derive first.** The observatory reads the engine SQLite directly and Hermes
polls the API, so the *physical* column flip (dropping ``status``/``mode``) must
land in the same coordinated cutover as those two repos (step 6). Exposing the new
vocabulary as a derivation now lets `/decision`, the observatory, and Hermes move
to it safely; the final step then swaps the storage underneath without changing
this contract. This is a bridge, not a permanent shim — at cutover the fields
below become real columns and these mappings collapse into the store.

Mapping
-------
``status``          → ``lifecycle`` + ``outcome``:
  running / pending           → lifecycle=running
  waiting_direction/approval/routing_review → lifecycle=paused  (which gate = position)
  completed                   → lifecycle=done, outcome=completed
  killed                      → lifecycle=done, outcome=stopped
  failed                      → lifecycle=done, outcome=failed
``mode`` (depth)    → ``target`` position (via the stage registry)
``current_stage``   → ``position`` (furthest stage; inferred for terminal rows)
``ended_by``/``error`` → ``reason`` (why it stopped / the error)
"""

from __future__ import annotations

from app import pipeline

_LIFECYCLE = {
    "pending": "running",
    "running": "running",
    "waiting_direction": "paused",
    "waiting_approval": "paused",
    "waiting_routing_review": "paused",
    "completed": "done",
    "killed": "done",
    "failed": "done",
}

_OUTCOME = {
    "completed": "completed",
    "killed": "stopped",
    "failed": "failed",
}


def lifecycle(status: str | None) -> str:
    return _LIFECYCLE.get(status or "", "running")


def outcome(status: str | None) -> str | None:
    """The terminal outcome, or None while the run is still live."""
    return _OUTCOME.get(status or "")


def target(run: dict) -> str | None:
    """The position the run is going to (from its chosen depth)."""
    mode = run.get("mode")
    return pipeline.position_for_depth(mode) if mode else None


def position(run: dict) -> str | None:
    """The furthest position the run reached.

    ``current_stage`` holds it live; it is cleared on finalize, so infer it for
    terminal rows: a completed run reached its target; a failed run stopped at
    ``failed_stage``; a killed run has no single furthest stage.
    """
    live = run.get("current_stage")
    if live:
        return live
    status = run.get("status")
    if status == "completed":
        return target(run)
    if status == "failed":
        return run.get("failed_stage")
    return None


def reason(run: dict) -> str | None:
    """Why the run stopped (or the error), or None for live/completed runs."""
    status = run.get("status")
    if status == "failed":
        return run.get("error")
    if status == "killed":
        # ended_by carries the stop kind: rejected / kill_confirmed /
        # kill_overridden / voided.
        return run.get("ended_by")
    return None


def project(run: dict) -> dict:
    """The canonical (position, lifecycle) view of a run, for API responses.

    `target` is deliberately not surfaced — it is a 1:1 projection of `depth`
    (archive→s1 … decide→s7), so exposing it would re-introduce a redundant name.
    The `target()` helper stays for internal position inference only.
    """
    status = run.get("status")
    return {
        "lifecycle": lifecycle(status),
        "position": position(run),
        "outcome": outcome(status),
        "reason": reason(run),
    }
