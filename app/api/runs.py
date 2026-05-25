import json
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_engine
from app.factory import PMEngine

router = APIRouter(prefix="/runs", tags=["runs"])


class _DummyPersona:
    """Sentinel used when a persona is unexpectedly absent from S4 output."""
    score = 0


class RunStartRequest(BaseModel):
    signal_id: str
    product_id: str
    mode: Optional[str] = None  # If provided, skip awaiting_direction and run immediately


class RunResponse(BaseModel):
    run_id: str
    product_id: str
    signal_id: str
    status: str
    current_stage: Optional[str]
    mode: Optional[str]
    recommendation_json: Optional[str]
    routing: Optional[str]
    composite_score: Optional[float]
    created_at: str
    completed_at: Optional[str]
    stage_outputs: Optional[list[dict]] = None


@router.post("/start", response_model=RunResponse, status_code=202)
async def start_run(
    body: RunStartRequest,
    background_tasks: BackgroundTasks,
    engine: PMEngine = Depends(get_engine),
) -> RunResponse:
    signal = engine.store.get_signal(body.signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail="Signal not found")

    if body.mode is not None:
        _validate_mode(body.mode)

    run_id = engine.store.create_run(
        product_id=body.product_id,
        signal_id=body.signal_id,
    )
    engine.store.update_run(run_id, status="running", current_stage="s1")

    background_tasks.add_task(
        _execute_s1_s2,
        run_id,
        body.signal_id,
        body.product_id,
        body.mode,
        engine,
    )

    run = engine.store.get_run(run_id)
    return RunResponse(**run)  # type: ignore[arg-type]


@router.get("/{run_id}", response_model=RunResponse)
async def get_run(
    run_id: str,
    include_outputs: bool = False,
    engine: PMEngine = Depends(get_engine),
) -> RunResponse:
    run = engine.store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    stage_outputs = None
    if include_outputs:
        stage_outputs = engine.store.get_all_stage_outputs(run_id)

    return RunResponse(**run, stage_outputs=stage_outputs)


@router.get("", response_model=list[RunResponse])
async def list_runs(
    product_id: Optional[str] = None,
    status: Optional[str] = None,
    routing: Optional[str] = None,
    limit: int = 50,
    engine: PMEngine = Depends(get_engine),
) -> list[RunResponse]:
    runs = engine.store.list_runs(
        product_id=product_id, status=status, routing=routing, limit=limit
    )
    return [RunResponse(**r) for r in runs]


def _validate_mode(mode: str) -> None:
    valid = {"file", "brief", "opportunity", "evaluate", "decide"}
    if mode not in valid:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid mode '{mode}'. Must be one of: {', '.join(sorted(valid))}",
        )


async def _execute_s1_s2(
    run_id: str,
    signal_id: str,
    product_id: str,
    requested_mode: Optional[str],
    engine: PMEngine,
) -> None:
    """Run Stage 1 + Stage 2, then either pause for direction or continue immediately."""
    from app.logging import emit_event
    from app.models.stages import RunContext, S1Input, S2Input
    from app.stages import s1_signal, s2_insight

    try:
        signal = engine.store.get_signal(signal_id)
        if signal is None:
            raise ValueError(f"Signal {signal_id} not found")

        full_context = engine.context_loader.load_full_context(product_id)
        context = RunContext(
            run_id=run_id,
            product_id=product_id,
            pm_identity=full_context.pm_identity,
            company_context=full_context.company_context,
            product_context=full_context.product_context,
        )

        engine.store.update_run(run_id, current_stage="s1")
        s1_out = await s1_signal.run(
            input=S1Input(
                signal_id=signal_id,
                title=signal["title"],
                raw_content=signal["raw_content"],
                source_url=signal["source_url"],
                source_type=signal["source_type"],
            ),
            context=context,
            llm=engine.llm,
            store=engine.store,
        )

        engine.store.update_run(run_id, current_stage="s2")
        s2_out = await s2_insight.run(
            input=S2Input(
                signal_id=signal_id,
                s1_output=s1_out.output,
                product_id=product_id,
            ),
            context=context,
            llm=engine.llm,
            store=engine.store,
        )

        # Store S2 recommendation so the API caller can display it
        recommendation = {
            "suggested_mode": s2_out.output.suggested_mode,
            "reasoning": s2_out.output.suggestion_reasoning,
            "relevance_score": s2_out.output.relevance_score,
        }
        engine.store.update_run(run_id, recommendation_json=json.dumps(recommendation))

        # Auto-triage: if relevance score is below threshold, skip Gate 1 entirely.
        # The signal is archived to the wiki kills folder and the run completes silently.
        from config import settings as _cfg
        from app.services.run_finalizer import finalize_run
        if s2_out.output.relevance_score < _cfg.AUTO_TRIAGE_THRESHOLD:
            engine.store.update_run(run_id, mode="file")  # set mode before finalize
            finalize_run(
                run_id, "completed", engine,
                event_action="auto_triaged",
                event_detail={
                    "relevance_score": s2_out.output.relevance_score,
                    "threshold": _cfg.AUTO_TRIAGE_THRESHOLD,
                    "reasoning": s2_out.output.suggestion_reasoning,
                },
            )
            _archive_auto_triaged_if_enabled(
                run_id=run_id,
                product_id=product_id,
                signal_title=signal["title"],
                s2_output=s2_out.output,
                settings_obj=_cfg,
            )
            return

        if requested_mode is not None:
            # Mode was specified upfront — skip the awaiting_direction pause
            chosen_mode = requested_mode
        else:
            # Pause and wait for the PM to confirm or override the suggested mode
            engine.store.update_run(
                run_id,
                status="awaiting_direction",
                current_stage="s2",
            )
            emit_event("run", "awaiting_direction", run_id, recommendation)
            await engine.notifier.send_gate1(
                run_id=run_id,
                product_id=product_id,
                signal_title=signal["title"],
                relevance_score=s2_out.output.relevance_score,
                suggested_mode=s2_out.output.suggested_mode,
                reasoning=s2_out.output.suggestion_reasoning,
            )
            return

        # Continue immediately with the chosen mode
        engine.store.update_run(run_id, mode=chosen_mode, status="running")
        emit_event("run", "direction_set", run_id, {"mode": chosen_mode})
        await _continue_after_direction(run_id, chosen_mode, context, engine)

    except Exception as exc:  # noqa: BLE001
        from app.services.run_finalizer import finalize_run
        finalize_run(run_id, "failed", engine, event_detail={"error": str(exc)})


async def _continue_after_direction(
    run_id: str,
    mode: str,
    context,
    engine: PMEngine,
) -> None:
    """Execute the stages appropriate for the chosen mode after S1+S2 are done."""
    from app.logging import emit_event
    from app.models.stages import S2OutputData, S3Input, S4Input, S7Input
    from app.stages import s3_opportunity, s4_evaluation, s7_summary
    from app.services.run_finalizer import finalize_run
    import json as _json

    try:
        s2_raw = engine.store.get_stage_output(run_id, "s2")
        if s2_raw is None:
            raise ValueError("S2 output not found")
        s2_output_data = S2OutputData(**_json.loads(s2_raw["output_json"])["output"])

        if mode == "file":
            finalize_run(run_id, "completed", engine, event_detail={"mode": "file"})
            return

        if mode == "brief":
            engine.store.update_run(run_id, current_stage="s7")
            await s7_summary.run(S7Input(mode="brief"), context, engine.llm, engine.store)
            finalize_run(run_id, "completed", engine, event_detail={"mode": "brief"})
            return

        # All remaining modes need Stage 3
        s2_signal_id = _json.loads(s2_raw["output_json"]).get("signal_id", run_id)
        engine.store.update_run(run_id, current_stage="s3")
        s3_out = await s3_opportunity.run(
            input=S3Input(
                signal_id=s2_signal_id,
                s2_output=s2_output_data,
                product_id=context.product_id,
            ),
            context=context,
            llm=engine.llm,
            store=engine.store,
        )

        if mode == "opportunity":
            finalize_run(run_id, "completed", engine, event_detail={"mode": "opportunity"})
            return

        # evaluate + decide both need Stage 4
        engine.store.update_run(run_id, current_stage="s4")
        s4_out = await s4_evaluation.run(
            S4Input(s3_output=s3_out.output),
            context,
            engine.llm,
            engine.store,
        )

        if mode == "evaluate":
            finalize_run(run_id, "completed", engine, event_detail={"mode": "evaluate"})
            return

        # decide mode: pause for human approval at Stage 4
        engine.store.update_run(run_id, status="waiting_approval", current_stage="s4")
        emit_event("run", "waiting_approval", run_id)

        # Notify PM via configured providers (Telegram / Slack)
        from config import settings as _notify_cfg
        signal_for_notify = engine.store.get_signal(
            _json.loads(s2_raw["output_json"]).get("signal_id", "")
        )
        signal_title = signal_for_notify["title"] if signal_for_notify else run_id
        personas = {p.persona: p for p in s4_out.output.personas}
        skeptic_concern = (
            personas["skeptic"].key_argument if "skeptic" in personas else ""
        )
        review_url = f"{_notify_cfg.BASE_URL}/runs/{run_id}/review"
        await engine.notifier.send_gate2(
            run_id=run_id,
            product_id=context.product_id,
            signal_title=signal_title,
            explorer_score=personas.get("explorer", _DummyPersona).score,
            strategist_score=personas.get("strategist", _DummyPersona).score,
            builder_score=personas.get("builder", _DummyPersona).score,
            skeptic_score=personas.get("skeptic", _DummyPersona).score,
            key_concern=skeptic_concern,
            review_url=review_url,
        )

    except Exception as exc:  # noqa: BLE001
        finalize_run(run_id, "failed", engine, event_detail={"error": str(exc)})


def _archive_auto_triaged_if_enabled(
    run_id: str,
    product_id: str,
    signal_title: str,
    s2_output,  # S2OutputData — avoid circular import at module level
    settings_obj,
) -> None:
    """Archive auto-triaged signals locally only while the legacy cutover flag is on."""
    if not settings_obj.AUTO_TRIAGE_LOCAL_ARCHIVE_ENABLED:
        return

    _archive_auto_triaged(
        run_id=run_id,
        product_id=product_id,
        signal_title=signal_title,
        s2_output=s2_output,
        wiki_root=settings_obj.WIKI_ROOT,
    )


def _archive_auto_triaged(
    run_id: str,
    product_id: str,
    signal_title: str,
    s2_output,  # S2OutputData — avoid circular import at module level
    wiki_root: str,
) -> None:
    """Delegate to wiki_sync.archive_auto_triaged; swallow OSError so run never fails."""
    from app.logging import emit_event
    from app.services.wiki_sync import archive_auto_triaged

    try:
        path = archive_auto_triaged(
            run_id=run_id,
            product_id=product_id,
            signal_title=signal_title,
            s2_output=s2_output,
            wiki_root=wiki_root,
        )
        emit_event("wiki_sync", "auto_triaged_archived", run_id, {"path": str(path)})
    except OSError:
        # Wiki root not mounted — non-fatal; already logged inside wiki_sync.
        pass
