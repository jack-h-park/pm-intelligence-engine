"""Routing Review Gate — confirm or override a routing decision after Stage 5.

Fires for all Stage 5 routing outcomes (prd, poc, kill). The PM sees the composite
score and assumption list, then confirms or overrides before Stage 6 starts.

State transitions:
  waiting_routing_review + confirm (kill)     → killed
  waiting_routing_review + confirm (poc/prd)  → running (Stage 6 starts)
  waiting_routing_review + override           → running (Stage 6 with overridden routing)
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_engine
from app.factory import PMEngine

router = APIRouter(prefix="/runs", tags=["routing-review"])


class RoutingReviewRequest(BaseModel):
    action: str  # "confirm" or "override"
    routing: str | None = None  # required when action == "override": "poc" | "prd" | "kill"
    reason: str | None = None  # optional PM note


def _require_waiting_routing_review(run_id: str, engine: PMEngine) -> dict:
    run = engine.store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    # Gate 3 = paused at s5 (canonical state; US-55).
    if not (run.get("lifecycle") == "paused" and run.get("position") == "s5"):
        raise HTTPException(
            status_code=409,
            detail=f"Run is '{run.get('lifecycle')}@{run.get('position')}', "
            "expected paused at Gate 3 (s5)",
        )
    return run


@router.post("/{run_id}/routing-review", status_code=202)
async def routing_review(
    run_id: str,
    body: RoutingReviewRequest,
    background_tasks: BackgroundTasks,
    engine: PMEngine = Depends(get_engine),
) -> dict:
    if body.action not in ("confirm", "override"):
        raise HTTPException(
            status_code=422,
            detail="action must be 'confirm' or 'override'",
        )
    if body.action == "override":
        if body.routing not in ("poc", "prd", "kill"):
            raise HTTPException(
                status_code=422,
                detail="routing must be 'poc', 'prd', or 'kill' when action is 'override'",
            )

    run = _require_waiting_routing_review(run_id, engine)
    s5_recommended = run.get("routing")

    if body.action == "confirm":
        routing = run.get("routing", "kill")
        _record_routing_decision(engine, run_id, "confirm", routing, s5_recommended, body.reason)
        return await _apply_routing(
            run_id, routing, engine, background_tasks, body.reason, confirmed=True
        )

    # override: PM changes the routing from S5's recommendation
    effective_routing = body.routing
    _record_routing_decision(
        engine, run_id, "override", effective_routing, s5_recommended, body.reason
    )
    engine.store.update_run(run_id, routing=effective_routing)
    return await _apply_routing(
        run_id, effective_routing, engine, background_tasks, body.reason, confirmed=False
    )


def _record_routing_decision(engine, run_id, action, chosen, recommended, reason):
    """Persist the Gate 3 decision as a labeled calibration datapoint (US-44):
    what S5 recommended vs what the PM chose."""
    note = f"chose={chosen}; recommended={recommended}"
    if reason:
        note += f"; reason={reason}"
    engine.store.record_approval(run_id=run_id, stage="s5", action=action, feedback_text=note)


async def _apply_routing(
    run_id: str,
    routing: str,
    engine: PMEngine,
    background_tasks: BackgroundTasks,
    reason: str | None,
    confirmed: bool,
) -> dict:
    """Apply an effective routing: kill finalizes immediately; poc/prd starts Stage 6."""
    from app.services.run_finalizer import finalize_run

    action_label = "routing_confirmed" if confirmed else "routing_overridden"

    if routing == "kill":
        await finalize_run(
            run_id,
            "killed",
            engine,
            event_action="kill_confirmed" if confirmed else "kill_overridden",
            event_detail={"reason": reason},
        )
        return {"run_id": run_id, "action": action_label, "routing": "kill"}

    # poc or prd — start Stage 6 in background
    engine.store.advance(run_id, "s6")
    background_tasks.add_task(_execute_s6_s7_with_routing, run_id, routing, engine)
    return {"run_id": run_id, "action": action_label, "routing": routing}


async def _execute_s6_s7_with_routing(run_id: str, routing: str, engine: PMEngine) -> None:
    """Segment 3 of a decide run: run S6 (PoC plan or PRD) then S7, then complete.

    The S6 branch (s6a for poc, s6b for prd) and the sequence come from the shared
    planner; ``runner.run_stage`` builds each stage's input from the store.
    """
    from app import runner
    from app.runner import plan_advance, target_for_depth
    from app.services.run_finalizer import finalize_run

    try:
        run = engine.store.get_run(run_id)
        if run is None:
            return

        from app.services.run_context import load_run_context

        context = load_run_context(run_id, engine)

        # plan_advance("s5", decide, routing) -> run (s6a|s6b, s7), then complete.
        plan = plan_advance("s5", target_for_depth("decide"), routing=routing)
        for position in plan.run:
            await runner.run_stage(position, run_id, engine, context)

        await finalize_run(
            run_id,
            "completed",
            engine,
            event_detail={"routing_override": routing},
        )

    except Exception as exc:  # noqa: BLE001
        await finalize_run(run_id, "failed", engine, event_detail={"error": str(exc)})
