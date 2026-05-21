"""Direction Gate API — confirm or override the pipeline depth after Stage 2.

The PM receives Stage 2's suggested_mode and reasoning, then calls this endpoint
to confirm or pick a different mode. This triggers the appropriate downstream stages.

State transition:
  awaiting_direction + POST /direction { mode } → running (stages for chosen mode)
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_engine
from app.factory import PMEngine

router = APIRouter(prefix="/runs", tags=["direction"])

_VALID_MODES = {"file", "brief", "opportunity", "evaluate", "decide"}


class DirectionRequest(BaseModel):
    mode: str  # One of: file | brief | opportunity | evaluate | decide


@router.post("/{run_id}/direction", status_code=202)
async def set_direction(
    run_id: str,
    body: DirectionRequest,
    background_tasks: BackgroundTasks,
    engine: PMEngine = Depends(get_engine),
) -> dict:
    if body.mode not in _VALID_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid mode '{body.mode}'. Must be one of: {', '.join(sorted(_VALID_MODES))}",
        )

    run = engine.store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run["status"] != "awaiting_direction":
        raise HTTPException(
            status_code=409,
            detail=f"Run is '{run['status']}', expected 'awaiting_direction'",
        )

    engine.store.update_run(run_id, mode=body.mode, status="running")

    background_tasks.add_task(_execute_from_direction, run_id, body.mode, engine)

    return {
        "run_id": run_id,
        "mode": body.mode,
        "action": "direction_set",
    }


async def _execute_from_direction(run_id: str, mode: str, engine: PMEngine) -> None:
    from app.logging import emit_event
    from app.models.stages import RunContext

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

        emit_event("run", "direction_set", run_id, {"mode": mode})

        from app.api.runs import _continue_after_direction
        await _continue_after_direction(run_id, mode, context, engine)

    except Exception as exc:  # noqa: BLE001
        engine.store.update_run(run_id, status="failed")
        emit_event("run", "failed", run_id, {"error": str(exc)})
