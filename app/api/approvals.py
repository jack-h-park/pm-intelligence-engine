"""Evaluation Gate API — approve / revise / reject a Stage 4 evaluation.

Only applies to runs in 'decide' mode that have reached 'waiting_approval' status.
Other modes (archive, note, structure, evaluate) do not have an evaluation gate.

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
    # Gate 2 = paused at s4 (canonical state; US-55).
    if not (run.get("lifecycle") == "paused" and run.get("position") == "s4"):
        raise HTTPException(
            status_code=409,
            detail=f"Run is '{run.get('lifecycle')}@{run.get('position')}', "
                   "expected paused at Gate 2 (s4)",
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
    engine.store.advance(run_id, "s5")

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
    engine.store.advance(run_id, "s4")

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
    engine.store.update_run(run_id, routing=None)  # clear routing before finalizing

    from app.services.run_finalizer import finalize_run
    await finalize_run(
        run_id, "killed", engine,
        event_action="rejected",
        event_detail={"reason": body.reason},
    )

    return {"run_id": run_id, "action": "rejected"}


async def _execute_s5_to_s7(run_id: str, engine: PMEngine) -> None:
    """Segment 2 of a decide run: run S5 (Prioritization) then pause at Gate 3.

    The stage sequence comes from the shared planner; execution goes through
    ``runner.run_stage`` (which records the routing S5 computes). Naming kept for
    the background-task call site; it stops at Gate 3, not S7.
    """
    from app import runner
    from app.models.stages import RunContext
    from app.runner import plan_advance, target_for_depth
    from app.services.run_finalizer import finalize_run

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

        # plan_advance("s4", decide) -> run (s5,), pause at Gate 3 (s5).
        plan = plan_advance("s4", target_for_depth("decide"))
        for position in plan.run:
            await runner.run_stage(position, run_id, engine, context)

        await _pause_at_gate3(run_id, run, context, engine)

    except Exception as exc:  # noqa: BLE001
        await finalize_run(run_id, "failed", engine, event_detail={"error": str(exc)})


async def _pause_at_gate3(run_id: str, run: dict, context, engine: PMEngine) -> None:
    """Pause a decide run at Gate 3 (post-S5 routing review) and notify the PM.

    Reads the stored S5/S4 outputs rather than threading them in, so the caller is
    just "run the plan, then pause"."""
    import json

    from app.logging import emit_event
    from app.models.stages import S4OutputData, S5OutputData

    s5 = S5OutputData(**json.loads(engine.store.get_stage_output(run_id, "s5")["output_json"])["output"])
    s4 = S4OutputData(**json.loads(engine.store.get_stage_output(run_id, "s4")["output_json"])["output"])

    engine.store.pause(run_id, "s5")
    emit_event("run", "waiting_routing_review", run_id, {
        "routing": s5.routing,
        "composite": s5.composite_score,
        "blocking_count": s5.blocking_count,
    })
    signal = engine.store.get_signal(run["signal_id"])
    signal_title = signal["title"] if signal else run_id
    persona_lines = [
        f"{p.persona.capitalize()} ({p.dimension}) {p.score}/5 — {p.key_argument}"
        for p in s4.personas
    ]
    rubric_total = f"{s4.rubric.total_score}/12" if s4.rubric else None
    await engine.notifier.send_gate3(
        run_id=run_id,
        product_id=context.product_id,
        signal_title=signal_title,
        routing=s5.routing,
        composite_score=s5.composite_score,
        blocking_count=s5.blocking_count,
        assumptions=[a.model_dump() for a in s5.assumptions],
        persona_lines=persona_lines,
        rubric_total=rubric_total,
        closing_window=s5.closing_window,
    )


async def _execute_s4_retry(run_id: str, feedback: str, engine: PMEngine) -> None:
    from app.logging import emit_event
    from app.models.stages import RunContext, S4Input
    from app.stages import s4_evaluation
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

        # Load latest S3 output
        s3_raw = engine.store.get_stage_output(run_id, "s3")
        if s3_raw is None:
            raise ValueError("S3 output not found")

        from app.models.stages import S3OutputData
        s3_output_data = S3OutputData(**json.loads(s3_raw["output_json"])["output"])

        # Determine next version number
        existing_s4 = engine.store.get_stage_output(run_id, "s4")
        next_version = (
            (json.loads(existing_s4["output_json"]).get("version", 1) + 1)
            if existing_s4 else 2
        )

        await s4_evaluation.run(
            S4Input(s3_output=s3_output_data, feedback=feedback, version=next_version),
            context,
            engine.llm,
            engine.store,
        )

        engine.store.pause(run_id, "s4")
        emit_event("run", "s4_retry_complete", run_id, {"version": next_version})

    except Exception as exc:  # noqa: BLE001
        await finalize_run(run_id, "failed", engine, event_detail={"error": str(exc)})
