import json

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from app.api.deps import get_engine
from app.factory import PMEngine

router = APIRouter(prefix="/runs", tags=["runs"])


class _DummyPersona:
    """Sentinel used when a persona is unexpectedly absent from S4 output."""
    score = 0


class RunStartRequest(BaseModel):
    # `depth` is the canonical processing-depth field (US-43); `mode` is still
    # accepted as a deprecated alias so existing clients (Hermes) keep working.
    model_config = ConfigDict(populate_by_name=True)
    signal_id: str
    # product_id is now OPTIONAL (US-49). Provided -> manual single-product start
    # (back-compat). Omitted -> Portfolio Triage fans out to the relevant products.
    product_id: str | None = None
    depth: str | None = Field(
        default=None, validation_alias=AliasChoices("depth", "mode")
    )  # If provided, skip waiting_direction and run immediately


class RunResponse(BaseModel):
    run_id: str
    product_id: str
    signal_id: str
    batch_id: str | None = None  # fan-out sibling group (US-49); null for single runs
    status: str
    current_stage: str | None
    depth: str | None = None  # processing depth (canonical, US-43)
    mode: str | None = None   # deprecated alias of `depth` (kept for existing clients)
    recommendation_json: str | None
    routing: str | None
    composite_score: float | None
    created_at: str
    updated_at: str | None = None  # bumped on every change — gate-watcher dedup
    completed_at: str | None
    stage_outputs: list[dict] | None = None
    gate1_review: dict | None = None
    gate3_review: dict | None = None

    @model_validator(mode="before")
    @classmethod
    def _mirror_depth_mode(cls, data):
        # The store dict uses "mode"; mirror it to "depth" (and vice-versa) so
        # responses always carry both during the transition.
        if isinstance(data, dict):
            d = dict(data)
            if d.get("depth") is None and d.get("mode") is not None:
                d["depth"] = d["mode"]
            elif d.get("mode") is None and d.get("depth") is not None:
                d["mode"] = d["depth"]
            return d
        return data


class BatchStartResponse(BaseModel):
    """Response for a Portfolio Triage fan-out start (US-49).

    Returned only when /runs/start is called without a product_id. A manual
    single-product start still returns a plain RunResponse (back-compat).
    """

    batch_id: str
    runs: list[RunResponse]
    triage: list[dict]  # per-product verdicts (product_id, relevance_score, reason, relevant)


class ScanResponse(BaseModel):
    """Response for a manual Portfolio Scan (US-49, C-3).

    ``batch_id`` is null when no other product cleared the threshold (the origin
    run is left untouched, not pulled into a batch). ``runs`` are the newly
    spawned sibling runs (the origin run is not repeated here).
    """

    scanned_run_id: str
    batch_id: str | None
    runs: list[RunResponse]
    triage: list[dict]


def _build_gate3_review(run_id: str, engine: PMEngine) -> dict | None:
    """Assemble the Gate 3 review payload from stored S4/S5 outputs.

    Returns None until S5 has run. Present on every response thereafter so the
    routing decision context stays inspectable after confirm/override.
    """
    s5_raw = engine.store.get_stage_output(run_id, "s5")
    if s5_raw is None:
        return None
    s5 = json.loads(s5_raw["output_json"])["output"]
    review: dict = {
        "routing": s5.get("routing"),
        "composite_score": s5.get("composite_score"),
        "blocking_count": s5.get("blocking_count"),
        "assumptions": s5.get("assumptions", []),
        "rationale": s5.get("rationale"),
        "closing_window": s5.get("closing_window", False),
    }
    s4_raw = engine.store.get_stage_output(run_id, "s4")
    if s4_raw is not None:
        s4 = json.loads(s4_raw["output_json"])["output"]
        review["personas"] = [
            {
                "persona": p.get("persona"),
                "dimension": p.get("dimension"),
                "score": p.get("score"),
                "key_argument": p.get("key_argument"),
            }
            for p in s4.get("personas", [])
        ]
        rubric = s4.get("rubric") or {}
        if rubric.get("total_score") is not None:
            review["rubric_total"] = f"{rubric['total_score']}/12"
    return review


def _build_gate1_review(run_id: str, engine: PMEngine) -> dict | None:
    """Assemble the Gate 1 review payload from stored S1/S2 outputs (US-46).

    Returns None until S2 has run. Gives the PM the full insight needed to
    decide the processing depth — not just relevance + suggestion.
    """
    s2_raw = engine.store.get_stage_output(run_id, "s2")
    if s2_raw is None:
        return None
    from app.modes import normalize_mode
    s2 = json.loads(s2_raw["output_json"])["output"]
    review: dict = {
        "relevance_score": s2.get("relevance_score"),
        "suggested_depth": normalize_mode(s2.get("suggested_mode")),
        "reasoning": s2.get("suggestion_reasoning"),
        "what_changed": s2.get("what_changed"),
        "reframing": s2.get("reframing"),
        "relevance_explanation": s2.get("relevance_explanation"),
        "pillar_references": s2.get("pillar_references", []),
    }
    s1_raw = engine.store.get_stage_output(run_id, "s1")
    if s1_raw is not None:
        s1 = json.loads(s1_raw["output_json"])["output"]
        review["signal_summary"] = s1.get("summary")
    return review


@router.post("/start", response_model=None, status_code=202)
async def start_run(
    body: RunStartRequest,
    background_tasks: BackgroundTasks,
    engine: PMEngine = Depends(get_engine),
) -> RunResponse | BatchStartResponse:
    """Start a run.

    Two modes (US-49):
    - **Manual** — `product_id` provided: a single run for that product. Returns a
      plain ``RunResponse`` (back-compat). The product is no longer required to
      match the signal's origin hint; it just has to exist.
    - **Fan-out** — `product_id` omitted: Portfolio Triage scores the signal
      against the portfolio and a run is started for each relevant product.
      Returns a ``BatchStartResponse``.
    """
    signal = engine.store.get_signal(body.signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail="Signal not found")

    # `depth` (canonical) accepts the `mode` alias; legacy values are normalized (US-43)
    from app.modes import normalize_mode
    requested_mode = normalize_mode(body.depth)
    if requested_mode is not None:
        _validate_mode(requested_mode)

    if body.product_id is not None:
        return _start_manual_run(
            body.signal_id, body.product_id, requested_mode, background_tasks, engine
        )
    return await _start_fanout_runs(
        body.signal_id, signal, requested_mode, background_tasks, engine
    )


def _start_manual_run(
    signal_id: str,
    product_id: str,
    requested_mode: str | None,
    background_tasks: BackgroundTasks,
    engine: PMEngine,
) -> RunResponse:
    _validate_product_exists(product_id, engine)
    if requested_mode is not None:
        validate_mode_for_product(requested_mode, product_id)

    run_id = engine.store.create_run(product_id=product_id, signal_id=signal_id)
    engine.store.update_run(run_id, status="running", current_stage="s1")
    engine.store.update_signal_status(signal_id, "in_run")
    background_tasks.add_task(
        _execute_s1_s2, run_id, signal_id, product_id, requested_mode, engine
    )
    return RunResponse(**engine.store.get_run(run_id))  # type: ignore[arg-type]


async def _start_fanout_runs(
    signal_id: str,
    signal: dict,
    requested_mode: str | None,
    background_tasks: BackgroundTasks,
    engine: PMEngine,
) -> BatchStartResponse:
    from config import settings
    from app.logging import emit_event
    from app.stages import portfolio_triage

    triage = await portfolio_triage.run(
        signal_id=signal_id,
        title=signal["title"],
        summary=signal["raw_content"],
        profiles=engine.context_loader.load_portfolio_profiles(),
        llm=engine.llm,
        threshold=settings.TRIAGE_RELEVANCE_THRESHOLD,
        pm_identity=engine.context_loader.load_pm_identity(),
    )

    batch_id = engine.store.create_batch(signal_id)
    runs = _spawn_runs_in_batch(
        triage.relevant_product_ids, signal_id, batch_id, requested_mode,
        background_tasks, engine,
    )
    if runs:
        engine.store.update_signal_status(signal_id, "in_run")
    # Membership is final immediately for product-agnostic fan-out (all relevant
    # products are known upfront). Closing it lets the Variant 2 synthesis trigger
    # fire once all runs settle.
    engine.store.close_batch_membership(batch_id)

    emit_event(
        "run", "fanout_started", signal_id,
        {"batch_id": batch_id, "relevant": triage.relevant_product_ids},
    )
    return BatchStartResponse(
        batch_id=batch_id,
        runs=runs,
        triage=[p.model_dump() for p in triage.products],
    )


def _spawn_runs_in_batch(
    product_ids: list[str],
    signal_id: str,
    batch_id: str,
    requested_mode: str | None,
    background_tasks: BackgroundTasks,
    engine: PMEngine,
) -> list[RunResponse]:
    """Create one run per product in the batch and start each pipeline."""
    runs: list[RunResponse] = []
    for product_id in product_ids:
        run_id = engine.store.create_run(
            product_id=product_id, signal_id=signal_id, batch_id=batch_id
        )
        engine.store.update_run(run_id, status="running", current_stage="s1")
        background_tasks.add_task(
            _execute_s1_s2, run_id, signal_id, product_id, requested_mode, engine
        )
        runs.append(RunResponse(**engine.store.get_run(run_id)))  # type: ignore[arg-type]
    return runs


@router.get("/batch/{batch_id}", response_model=None)
async def get_batch(
    batch_id: str,
    engine: PMEngine = Depends(get_engine),
) -> dict:
    """A fan-out batch: its sibling runs plus the portfolio synthesis (US-49).

    ``synthesis`` is null until every run in the batch has settled.
    """
    batch = engine.store.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")

    runs = engine.store.list_runs(batch_id=batch_id, limit=1000)
    synthesis = engine.store.get_portfolio_synthesis(batch_id)
    if synthesis is not None:
        synthesis = {**synthesis, "content": json.loads(synthesis["content_json"])}

    return {
        "batch_id": batch_id,
        "signal_id": batch["signal_id"],
        "membership_closed": batch["membership_closed"],
        "runs": [RunResponse(**r).model_dump() for r in runs],
        "synthesis": synthesis,
    }


@router.post("/{run_id}/scan", response_model=None, status_code=202)
async def scan_portfolio(
    run_id: str,
    background_tasks: BackgroundTasks,
    engine: PMEngine = Depends(get_engine),
) -> ScanResponse:
    """Manual Portfolio Scan (US-49, C-3) — human-in-the-loop fan-out.

    For a run started manually for one product, check whether the same signal is
    relevant to *other* products and fan out to those. This is a deliberate PM
    action (surfaced as a button on the Gate 1 review), not automatic — and it
    works whether the origin run is paused at Gate 1 or already auto-triaged.

    Portfolio Triage runs over the portfolio minus the origin product. If any
    other product is relevant, the origin run is pulled into a new batch
    alongside the new sibling runs; membership is closed so Variant 2 synthesis
    fires once they all settle. If nothing else is relevant, the origin run is
    left untouched.
    """
    from config import settings
    from app.logging import emit_event
    from app.stages import portfolio_triage

    run = engine.store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.get("batch_id"):
        raise HTTPException(
            status_code=409,
            detail=f"Run is already part of batch '{run['batch_id']}'",
        )
    signal = engine.store.get_signal(run["signal_id"])
    if signal is None:
        raise HTTPException(status_code=404, detail="Signal not found")

    origin_product = run["product_id"]
    triage = await portfolio_triage.run(
        signal_id=run["signal_id"],
        title=signal["title"],
        summary=signal["raw_content"],
        profiles=engine.context_loader.load_portfolio_profiles(exclude=(origin_product,)),
        llm=engine.llm,
        threshold=settings.TRIAGE_RELEVANCE_THRESHOLD,
        pm_identity=engine.context_loader.load_pm_identity(),
    )

    if not triage.relevant_product_ids:
        emit_event("run", "scan_no_match", run_id, {"product_id": origin_product})
        return ScanResponse(
            scanned_run_id=run_id, batch_id=None, runs=[],
            triage=[p.model_dump() for p in triage.products],
        )

    batch_id = engine.store.create_batch(run["signal_id"])
    engine.store.update_run(run_id, batch_id=batch_id)  # pull the origin run in
    runs = _spawn_runs_in_batch(
        triage.relevant_product_ids, run["signal_id"], batch_id, None,
        background_tasks, engine,
    )
    # Membership closes now: the scan decision is resolved, so the synthesis
    # trigger may fire once the origin run and all siblings settle.
    engine.store.close_batch_membership(batch_id)

    emit_event(
        "run", "scan_fanout", run_id,
        {"batch_id": batch_id, "relevant": triage.relevant_product_ids},
    )
    return ScanResponse(
        scanned_run_id=run_id, batch_id=batch_id, runs=runs,
        triage=[p.model_dump() for p in triage.products],
    )


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

    return RunResponse(
        **run,
        stage_outputs=stage_outputs,
        gate1_review=_build_gate1_review(run_id, engine),
        gate3_review=_build_gate3_review(run_id, engine),
    )


@router.get("", response_model=list[RunResponse])
async def list_runs(
    product_id: str | None = None,
    status: str | None = None,
    routing: str | None = None,
    event: str | None = None,
    since: str | None = None,
    limit: int = 50,
    engine: PMEngine = Depends(get_engine),
) -> list[RunResponse]:
    since_dt = None
    if since is not None:
        from datetime import datetime
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid 'since' value '{since}' — expected ISO 8601",
            )
    if event is not None:
        valid_events = {
            "approve", "revise", "reject", "auto_triaged", "reopen",
            "direction", "confirm", "override",
        }
        if event not in valid_events:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid event '{event}'. Must be one of: {', '.join(sorted(valid_events))}",
            )
    runs = engine.store.list_runs(
        product_id=product_id,
        status=status,
        routing=routing,
        event=event,
        since=since_dt,
        limit=limit,
    )
    return [RunResponse(**r) for r in runs]


@router.post("/{run_id}/reopen", response_model=RunResponse)
async def reopen_run(
    run_id: str,
    engine: PMEngine = Depends(get_engine),
) -> RunResponse:
    """Revive an auto-triaged run to waiting_direction (US-31).

    Only runs that were silently filed by the relevance gate are revivable —
    a deliberate PM decision (file at Gate 1, reject at Gate 2, kill at Gate 3)
    is not undone by this endpoint.
    """
    from app.logging import emit_event

    run = engine.store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    events = engine.store.get_approval_events(run_id)
    if not any(e["action"] == "auto_triaged" for e in events):
        raise HTTPException(
            status_code=409,
            detail="Only auto-triaged runs can be reopened",
        )
    if run["status"] != "completed":
        raise HTTPException(
            status_code=409,
            detail=f"Run is '{run['status']}', expected 'completed' "
                   "(already reopened runs cannot be reopened again)",
        )

    engine.store.record_approval(run_id=run_id, stage="s2", action="reopen")
    engine.store.update_run(
        run_id,
        status="waiting_direction",
        current_stage="s2",
        mode=None,
        completed_at=None,
    )
    engine.store.update_signal_status(run["signal_id"], "in_run")
    emit_event("run", "reopened", run_id, {"from": "auto_triaged"})

    updated = engine.store.get_run(run_id)
    return RunResponse(**updated, gate3_review=None)


def _validate_mode(mode: str) -> None:
    valid = {"archive", "note", "structure", "evaluate", "decide"}
    if mode not in valid:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid mode '{mode}'. Must be one of: {', '.join(sorted(valid))}",
        )


def _validate_product_exists(product_id: str, engine: PMEngine) -> None:
    """Raise 422 if the product has no context directory (US-49).

    Replaces the old strict equality gate: under 1:N the caller-supplied product
    no longer has to match the signal's origin hint — it just has to be real.
    """
    try:
        engine.context_loader.load_product_context(product_id)
    except FileNotFoundError:
        raise HTTPException(
            status_code=422, detail=f"Unknown product_id '{product_id}'"
        )


def validate_mode_for_product(mode: str, product_id: str) -> None:
    """Raise 422 if the mode is not allowed for the given product scope."""
    _GENERAL_ALLOWED = {"archive", "note"}
    if product_id == "general" and mode not in _GENERAL_ALLOWED:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Mode '{mode}' is not allowed for product_id 'general'. "
                "general signals support only file/brief. "
                "Reassign this signal to a specific product for opportunity/evaluate/decide."
            ),
        )


async def _execute_s1_s2(
    run_id: str,
    signal_id: str,
    product_id: str,
    requested_mode: str | None,
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

        from app.services.run_finalizer import finalize_run
        from config import settings as _cfg

        if requested_mode is not None:
            # PM explicitly specified a depth at run-start — always honor it.
            # Skip auto-triage: PM's stated intent overrides the S2 relevance score.
            chosen_mode = requested_mode
        elif s2_out.output.relevance_score < _cfg.AUTO_TRIAGE_THRESHOLD:
            # No depth specified and relevance is below threshold — auto-triage.
            engine.store.update_run(run_id, mode="archive")  # set mode before finalize
            # Durable marker so auto-triaged runs stay queryable and revivable
            # (GET /runs?event=auto_triaged, POST /runs/{id}/reopen — US-31)
            engine.store.record_approval(
                run_id=run_id,
                stage="s2",
                action="auto_triaged",
                feedback_text=s2_out.output.suggestion_reasoning,
            )
            await finalize_run(
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
        else:
            # No depth specified and relevance is acceptable — pause at Gate 1.
            engine.store.update_run(
                run_id,
                status="waiting_direction",
                current_stage="s2",
            )
            emit_event("run", "waiting_direction", run_id, recommendation)
            await engine.notifier.send_gate1(
                run_id=run_id,
                product_id=product_id,
                signal_title=signal["title"],
                relevance_score=s2_out.output.relevance_score,
                suggested_mode=s2_out.output.suggested_mode,
                reasoning=s2_out.output.suggestion_reasoning,
                what_changed=s2_out.output.what_changed,
                relevance_explanation=s2_out.output.relevance_explanation,
                pillar_references=s2_out.output.pillar_references,
            )
            return

        # Continue immediately with the chosen mode
        engine.store.update_run(run_id, mode=chosen_mode, status="running")
        emit_event("run", "direction_set", run_id, {"mode": chosen_mode})
        await _continue_after_direction(run_id, chosen_mode, context, engine)

    except Exception as exc:  # noqa: BLE001
        from app.services.run_finalizer import finalize_run
        await finalize_run(run_id, "failed", engine, event_detail={"error": str(exc)})


async def _continue_after_direction(
    run_id: str,
    mode: str,
    context,
    engine: PMEngine,
) -> None:
    """Execute the stages appropriate for the chosen mode after S1+S2 are done."""
    import json as _json

    from app.logging import emit_event
    from app.models.stages import S2OutputData, S3Input, S4Input, S7Input
    from app.services.run_finalizer import finalize_run
    from app.stages import s3_opportunity, s4_evaluation, s7_summary

    try:
        s2_raw = engine.store.get_stage_output(run_id, "s2")
        if s2_raw is None:
            raise ValueError("S2 output not found")
        s2_output_data = S2OutputData(**_json.loads(s2_raw["output_json"])["output"])

        if mode == "archive":
            await finalize_run(run_id, "completed", engine, event_detail={"mode": "archive"})
            return

        if mode == "note":
            engine.store.update_run(run_id, current_stage="s7")
            await s7_summary.run(S7Input(mode="note"), context, engine.llm, engine.store)
            await finalize_run(run_id, "completed", engine, event_detail={"mode": "note"})
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

        if mode == "structure":
            await finalize_run(run_id, "completed", engine, event_detail={"mode": "structure"})
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
            await finalize_run(run_id, "completed", engine, event_detail={"mode": "evaluate"})
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
        await finalize_run(run_id, "failed", engine, event_detail={"error": str(exc)})


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
