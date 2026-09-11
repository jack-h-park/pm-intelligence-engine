from typing import Any

# ruff: noqa: E501 — the action/gate table in the module docstring below is
# aligned by hand; wrapping it would break the alignment and make it harder
# to read, not easier.
"""Unified decision endpoint — ``POST /runs/{id}/decision`` (US-55 step 4).

One endpoint replaces the six per-gate/terminal ones. A run's human decision is
always one of four verbs, interpreted against where the run currently is:

  action       Gate 1 (waiting_direction)  Gate 2 (waiting_approval)  Gate 3 (waiting_routing_review)  completed
  ------------ --------------------------- -------------------------- ------------------------------- ---------
  advance      —                           approve                    confirm routing                 reopen (auto-triaged)
  advance_to   pick depth (target)         —                          override routing (routing)      deepen (target)
  revise       —                           re-run S4 (feedback)        —                               —
  stop         —                           reject (reason)             kill (reason)                   —
  stop (any non-terminal state)            —                          —                               —          void (reason)

This is a **facade** over the existing handlers during the transition: the old
endpoints stay live (the ops plane and dashboard still call them) and share this one
implementation. At the final cutover (step 6) the old routes are removed and the
consumers move to ``/decision``. The action vocabulary is schema-independent, so
it survives the step-5 flip to (position, lifecycle) unchanged.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_engine
from app.factory import PMEngine

router = APIRouter(prefix="/runs", tags=["decision"])

_ACTIONS = {"advance", "advance_to", "revise", "stop"}


class DecisionRequest(BaseModel):
    action: str  # advance | advance_to | revise | stop
    target: str | None = None  # depth for advance_to at Gate 1 / on a completed run (deepen)
    routing: str | None = None  # prd|poc|kill for advance_to at Gate 3 (override)
    reason: str | None = None  # for stop (reject / kill / void)
    feedback: str | None = None  # for revise
    origin: str | None = None  # who is deciding; absent = a human (see direction.py)


@router.post("/{run_id}/decision", status_code=202)
async def decide(
    run_id: str,
    body: DecisionRequest,
    background_tasks: BackgroundTasks,
    engine: PMEngine = Depends(get_engine),
) -> dict[str, Any]:
    if body.action not in _ACTIONS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid action '{body.action}'. Must be one of: {', '.join(sorted(_ACTIONS))}",
        )

    run = engine.store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    # Discriminate the run's state from the canonical (lifecycle, position, outcome)
    # columns (US-55 step 7d-1) rather than the retired `status` enum.
    lifecycle = run.get("lifecycle")
    position = run.get("position")
    outcome = run.get("outcome")
    state = f"{lifecycle}@{position}"  # human-readable label for error messages
    action = body.action

    # --- Gate 1 (paused@s2): pick the processing depth -------------------
    if lifecycle == "paused" and position == "s2":
        if action == "advance_to":
            from app.api.direction import DirectionRequest, set_direction

            return await set_direction(
                run_id,
                DirectionRequest(depth=body.target, origin=body.origin),
                background_tasks,
                engine,
            )
        raise _invalid(action, state, "advance_to {target}")

    # --- Gate 2 (paused@s4): approve / revise / reject -------------------
    if lifecycle == "paused" and position == "s4":
        from app.api.approvals import (
            RejectRequest,
            ReviseRequest,
            approve_run,
            reject_run,
            revise_run,
        )

        if action == "advance":
            return await approve_run(run_id, background_tasks, engine)
        if action == "revise":
            return await revise_run(
                run_id,
                ReviseRequest(feedback=body.feedback or body.reason or ""),
                background_tasks,
                engine,
            )
        if action == "stop":
            return await reject_run(run_id, RejectRequest(reason=body.reason or ""), engine)
        raise _invalid(action, state, "advance | revise | stop")

    # --- Gate 3 (paused@s5): confirm / override routing ------------------
    if lifecycle == "paused" and position == "s5":
        from app.api.routing_review import RoutingReviewRequest, routing_review

        if action == "advance":
            return await routing_review(
                run_id,
                RoutingReviewRequest(action="confirm", reason=body.reason),
                background_tasks,
                engine,
            )
        if action == "advance_to":
            return await routing_review(
                run_id,
                RoutingReviewRequest(action="override", routing=body.routing, reason=body.reason),
                background_tasks,
                engine,
            )
        if action == "stop":
            # A stop at Gate 3 is a routing kill, recorded via the override path.
            return await routing_review(
                run_id,
                RoutingReviewRequest(action="override", routing="kill", reason=body.reason),
                background_tasks,
                engine,
            )
        raise _invalid(action, state, "advance | advance_to {routing} | stop")

    # --- Completed (done/completed): deepen or reopen -------------------
    if lifecycle == "done" and outcome == "completed":
        if action == "advance_to":
            from app.api.deepen import DeepenRequest, deepen_run

            return await deepen_run(
                run_id, DeepenRequest(depth=body.target), background_tasks, engine
            )
        if action == "advance":
            # Revive an auto-triaged run back to Gate 1 (reopen validates eligibility).
            from app.api.runs import reopen_run

            resp = await reopen_run(run_id, engine)
            return resp.model_dump() if hasattr(resp, "model_dump") else resp
        raise _invalid(action, state, "advance_to {target} (deepen) | advance (reopen)")

    # --- Any other non-terminal state: administrative void ---------------
    if action == "stop" and lifecycle != "done":
        from app.api.void import VoidRequest, void_run

        return await void_run(run_id, VoidRequest(reason=body.reason or ""), engine)

    raise _invalid(action, state, "(none — run is terminal or state has no such decision)")


def _invalid(action: str, state: str, allowed: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=f"action '{action}' is not valid for a run in '{state}'. Allowed here: {allowed}",
    )
