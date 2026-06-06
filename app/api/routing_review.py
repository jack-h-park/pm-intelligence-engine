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
from typing import Optional

from app.api.deps import get_engine
from app.factory import PMEngine

router = APIRouter(prefix="/runs", tags=["routing-review"])


class RoutingReviewRequest(BaseModel):
    action: str  # "confirm" or "override"
    routing: Optional[str] = None  # required when action == "override": "poc" | "prd" | "kill"
    reason: Optional[str] = None  # optional PM note


def _require_waiting_routing_review(run_id: str, engine: PMEngine) -> dict:
    run = engine.store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run["status"] != "waiting_routing_review":
        raise HTTPException(
            status_code=409,
            detail=f"Run is '{run['status']}', expected 'waiting_routing_review'",
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

    if body.action == "confirm":
        routing = run.get("routing", "kill")
        return await _apply_routing(run_id, routing, engine, background_tasks, body.reason, confirmed=True)

    # override: PM changes the routing from S5's recommendation
    effective_routing = body.routing
    engine.store.update_run(run_id, routing=effective_routing)
    return await _apply_routing(run_id, effective_routing, engine, background_tasks, body.reason, confirmed=False)


async def _apply_routing(
    run_id: str,
    routing: str,
    engine: PMEngine,
    background_tasks: BackgroundTasks,
    reason: Optional[str],
    confirmed: bool,
) -> dict:
    """Apply an effective routing: kill finalizes immediately; poc/prd starts Stage 6."""
    from app.services.run_finalizer import finalize_run

    action_label = "routing_confirmed" if confirmed else "routing_overridden"

    if routing == "kill":
        finalize_run(
            run_id, "killed", engine,
            event_action="kill_confirmed" if confirmed else "kill_overridden",
            event_detail={"reason": reason},
        )
        return {"run_id": run_id, "action": action_label, "routing": "kill"}

    # poc or prd — start Stage 6 in background
    engine.store.update_run(run_id, status="running", current_stage="s6")
    background_tasks.add_task(_execute_s6_s7_with_routing, run_id, routing, engine)
    return {"run_id": run_id, "action": action_label, "routing": routing}


async def _execute_s6_s7_with_routing(run_id: str, routing: str, engine: PMEngine) -> None:
    from app.logging import emit_event
    from app.models.stages import RunContext, S6AInput, S6BInput, S7Input
    from app.stages import s6a_poc_plan, s6b_prd, s7_summary
    from app.services.run_finalizer import finalize_run
    import json

    try:
        run = engine.store.get_run(run_id)
        if run is None:
            return

        full_context = engine.context_loader.load_full_context(run["product_id"])
        context = RunContext(
            run_id=run_id,
            product_id=run["product_id"],
            pm_identity=full_context.pm_identity,
            company_context=full_context.company_context,
            product_context=full_context.product_context,
        )

        # Load S5 output for Stage 6 input
        s5_raw = engine.store.get_stage_output(run_id, "s5")
        if s5_raw is None:
            raise ValueError("S5 output not found")

        from app.models.stages import S5OutputData
        s5_output_data = S5OutputData(**json.loads(s5_raw["output_json"])["output"])

        if routing == "poc":
            engine.store.update_run(run_id, current_stage="s6a")
            s6_out = await s6a_poc_plan.run(
                S6AInput(s5_output=s5_output_data), context, engine.llm, engine.store
            )
            s7_in = S7Input(mode="decide", s5_output=s5_output_data, s6a_output=s6_out.output)
        else:
            engine.store.update_run(run_id, current_stage="s6b")
            s6_out = await s6b_prd.run(
                S6BInput(s5_output=s5_output_data), context, engine.llm, engine.store
            )
            s7_in = S7Input(mode="decide", s5_output=s5_output_data, s6b_output=s6_out.output)

        engine.store.update_run(run_id, current_stage="s7")
        await s7_summary.run(s7_in, context, engine.llm, engine.store)

        finalize_run(
            run_id, "completed", engine,
            event_detail={"routing_override": routing},
        )

    except Exception as exc:  # noqa: BLE001
        finalize_run(run_id, "failed", engine, event_detail={"error": str(exc)})
