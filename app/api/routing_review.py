"""Routing Review Gate — confirm or override a kill routing decision after Stage 5.

Only fires when Stage 5 routes to 'kill'. The PM sees the assumption list and
composite score, then either confirms the kill or overrides to poc/prd.

State transitions:
  waiting_routing_review + confirm  → killed
  waiting_routing_review + override → running (Stage 6 starts with overridden routing)
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.api.deps import get_engine
from app.factory import PMEngine

router = APIRouter(prefix="/runs", tags=["routing-review"])


class RoutingReviewRequest(BaseModel):
    action: str  # "confirm" or "override"
    routing: Optional[str] = None  # required when action == "override": "poc" | "prd"
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
        if body.routing not in ("poc", "prd"):
            raise HTTPException(
                status_code=422,
                detail="routing must be 'poc' or 'prd' when action is 'override'",
            )

    _require_waiting_routing_review(run_id, engine)

    if body.action == "confirm":
        engine.store.update_run(run_id, status="killed")
        from app.logging import emit_event
        emit_event("run", "kill_confirmed", run_id, {"reason": body.reason})
        return {"run_id": run_id, "action": "kill_confirmed"}

    # override: PM disagrees with blocking classification, proceed with chosen routing
    engine.store.update_run(run_id, routing=body.routing, status="running", current_stage="s6")
    background_tasks.add_task(_execute_s6_s7_with_routing, run_id, body.routing, engine)
    return {"run_id": run_id, "action": "routing_overridden", "routing": body.routing}


async def _execute_s6_s7_with_routing(run_id: str, routing: str, engine: PMEngine) -> None:
    from app.logging import emit_event
    from app.models.stages import RunContext, S6AInput, S6BInput, S7Input
    from app.stages import s6a_poc_plan, s6b_prd, s7_summary
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

        engine.store.update_run(run_id, status="completed", current_stage=None)
        emit_event("run", "completed", run_id, {"routing_override": routing})

    except Exception as exc:  # noqa: BLE001
        engine.store.update_run(run_id, status="failed")
        emit_event("run", "failed", run_id, {"error": str(exc)})
