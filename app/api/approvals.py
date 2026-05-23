"""Evaluation Gate API — approve / revise / reject a Stage 4 evaluation.

Only applies to runs in 'decide' mode that have reached 'waiting_approval' status.
Other modes (file, brief, opportunity, evaluate) do not have an evaluation gate.

State transitions:
  waiting_approval + approve → running (Stage 5 Prioritization starts)
  waiting_approval + revise  → running (Stage 4 Evaluation retries with PM feedback, version++)
  waiting_approval + reject  → killed
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_engine
from app.factory import PMEngine

router = APIRouter(prefix="/runs", tags=["approvals"])


class ReviseRequest(BaseModel):
    feedback: str


class RejectRequest(BaseModel):
    reason: str


def _require_waiting_approval(run_id: str, engine: PMEngine) -> dict:
    run = engine.store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.get("mode") != "decide":
        raise HTTPException(
            status_code=409,
            detail="Evaluation gate only applies to runs in 'decide' mode",
        )
    if run["status"] != "waiting_approval":
        raise HTTPException(
            status_code=409,
            detail=f"Run is '{run['status']}', expected 'waiting_approval'",
        )
    return run


@router.post("/{run_id}/approve", status_code=202)
async def approve_run(
    run_id: str,
    background_tasks: BackgroundTasks,
    engine: PMEngine = Depends(get_engine),
) -> dict:
    _require_waiting_approval(run_id, engine)

    engine.store.record_approval(run_id=run_id, stage="s4", action="approve")
    engine.store.update_run(run_id, status="running", current_stage="s5")

    background_tasks.add_task(_execute_s5_to_s7, run_id, engine)

    return {"run_id": run_id, "action": "approved"}


@router.post("/{run_id}/revise", status_code=202)
async def revise_run(
    run_id: str,
    body: ReviseRequest,
    background_tasks: BackgroundTasks,
    engine: PMEngine = Depends(get_engine),
) -> dict:
    _require_waiting_approval(run_id, engine)

    engine.store.record_approval(
        run_id=run_id, stage="s4", action="revise", feedback_text=body.feedback
    )
    engine.store.update_run(run_id, status="running", current_stage="s4")

    background_tasks.add_task(_execute_s4_retry, run_id, body.feedback, engine)

    return {"run_id": run_id, "action": "revise_queued"}


@router.post("/{run_id}/reject", status_code=200)
async def reject_run(
    run_id: str,
    body: RejectRequest,
    engine: PMEngine = Depends(get_engine),
) -> dict:
    _require_waiting_approval(run_id, engine)

    engine.store.record_approval(
        run_id=run_id, stage="s4", action="reject", feedback_text=body.reason
    )
    engine.store.update_run(run_id, status="killed", routing=None)

    from app.logging import emit_event
    emit_event("run", "rejected", run_id, {"reason": body.reason})

    return {"run_id": run_id, "action": "rejected"}


async def _execute_s5_to_s7(run_id: str, engine: PMEngine) -> None:
    from app.logging import emit_event
    from app.models.stages import RunContext, S5Input, S6AInput, S6BInput, S7Input
    from app.stages import s5_prioritization, s6a_poc_plan, s6b_prd, s7_summary
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

        # Load S4 output (latest version)
        s4_raw = engine.store.get_stage_output(run_id, "s4")
        if s4_raw is None:
            raise ValueError("S4 output not found")

        from app.models.stages import S4OutputData
        s4_output_data = S4OutputData(**json.loads(s4_raw["output_json"])["output"])

        engine.store.update_run(run_id, current_stage="s5")
        s5_out = await s5_prioritization.run(
            S5Input(s4_output=s4_output_data), context, engine.llm, engine.store
        )

        routing = s5_out.output.routing
        engine.store.update_run(run_id, routing=routing)

        if routing == "kill":
            # Pause for PM review — kill based on LLM assumption classification may be incorrect
            engine.store.update_run(run_id, status="waiting_routing_review", current_stage="s5")
            emit_event("run", "waiting_routing_review", run_id, {
                "composite": s5_out.output.composite_score,
                "blocking_count": s5_out.output.blocking_count,
            })
            return

        if routing == "poc":
            engine.store.update_run(run_id, current_stage="s6a")
            s6_out = await s6a_poc_plan.run(
                S6AInput(s5_output=s5_out.output), context, engine.llm, engine.store
            )
            s7_in = S7Input(s5_output=s5_out.output, s6a_output=s6_out.output)
        else:
            engine.store.update_run(run_id, current_stage="s6b")
            s6_out = await s6b_prd.run(
                S6BInput(s5_output=s5_out.output), context, engine.llm, engine.store
            )
            s7_in = S7Input(s5_output=s5_out.output, s6b_output=s6_out.output)

        engine.store.update_run(run_id, current_stage="s7")
        await s7_summary.run(s7_in, context, engine.llm, engine.store)

        engine.store.update_run(run_id, status="completed", current_stage=None)
        emit_event("run", "completed", run_id)

    except Exception as exc:  # noqa: BLE001
        engine.store.update_run(run_id, status="failed")
        emit_event("run", "failed", run_id, {"error": str(exc)})


async def _execute_s4_retry(run_id: str, feedback: str, engine: PMEngine) -> None:
    from app.logging import emit_event
    from app.models.stages import RunContext, S4Input
    from app.stages import s4_evaluation
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

        # Load latest S3 output
        s3_raw = engine.store.get_stage_output(run_id, "s3")
        if s3_raw is None:
            raise ValueError("S3 output not found")

        from app.models.stages import S3OutputData
        s3_output_data = S3OutputData(**json.loads(s3_raw["output_json"])["output"])

        # Determine next version number
        existing_s4 = engine.store.get_stage_output(run_id, "s4")
        next_version = (json.loads(existing_s4["output_json"]).get("version", 1) + 1) if existing_s4 else 2

        await s4_evaluation.run(
            S4Input(s3_output=s3_output_data, feedback=feedback, version=next_version),
            context,
            engine.llm,
            engine.store,
        )

        engine.store.update_run(run_id, status="waiting_approval", current_stage="s4")
        emit_event("run", "s4_retry_complete", run_id, {"version": next_version})

    except Exception as exc:  # noqa: BLE001
        engine.store.update_run(run_id, status="failed")
        emit_event("run", "failed", run_id, {"error": str(exc)})
