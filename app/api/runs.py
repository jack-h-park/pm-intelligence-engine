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
    # When true AND no depth is given, the run ALWAYS pauses at Gate 1 even if S2
    # relevance is below AUTO_TRIAGE_THRESHOLD — i.e. S2 auto-triage to archive is
    # suppressed for this run, so the PM always makes the direction decision. Set
    # by Hermes ops when the PM starts a run interactively; defaults False so the
    # autonomous/auto-triage path is unchanged. No effect when depth is provided.
    force_gate1: bool = False


class PromoteRequest(BaseModel):
    """Promote a deferred candidate into a real sibling run (US-49 §0).

    ``depth`` is optional; omitted, the promotion inherits the primary run's depth
    (or, if the primary has none yet, the promoted run takes its own Gate 1)."""
    model_config = ConfigDict(populate_by_name=True)
    product_id: str
    depth: str | None = Field(
        default=None, validation_alias=AliasChoices("depth", "mode")
    )


class RunResponse(BaseModel):
    run_id: str
    product_id: str
    signal_id: str
    batch_id: str | None = None  # fan-out sibling group (US-49); null for single runs
    # `status`/`current_stage`/`mode` are no longer surfaced (US-55 single
    # vocabulary): the run state is `lifecycle` + `position` + `target` +
    # `outcome` + `reason` (below); the depth name is `depth`. The legacy columns
    # still exist in storage (dual-write) but are an internal detail — the API,
    # observatory, and Hermes read the canonical fields only.
    depth: str | None = None  # processing-depth name (archive/note/…/decide)
    recommendation_json: str | None
    routing: str | None
    composite_score: float | None
    created_at: str
    updated_at: str | None = None  # bumped on every change — gate-watcher dedup
    completed_at: str | None
    # Retry lineage & failure diagnostics. attempt_no/root_run_id let clients
    # group retries of the same signal; failed_stage/error explain a failure
    # without grepping server.log.
    attempt_no: int | None = None
    root_run_id: str | None = None
    # "start" (default) or "refresh" — a run started by POST /signals/{id}/refresh
    # after the signal's content was re-ingested. Lets the observatory render a
    # re-ingest distinctly instead of as "attempt N of N" of a retry lineage.
    origin: str | None = None
    failed_stage: str | None = None
    error: str | None = None
    # Semantic terminal reason (auto_triaged/archived/noted/…/voided/failed);
    # null while non-terminal or for pre-column legacy rows. Lets a consumer read
    # "how did this end?" without joining status+mode+routing+approval_events.
    ended_by: str | None = None
    stage_outputs: list[dict] | None = None
    gate1_review: dict | None = None
    gate3_review: dict | None = None
    # Absolute link to the engine-served browser review page, built from BASE_URL
    # (the iMac's Tailscale address in production). Exposed so the delivery owner
    # (Iris) can include it in Gate 2 messages without knowing the engine's
    # network config — see docs/NOTIFICATION_CONTRACT.md §2.
    review_url: str | None = None
    # Canonical (position, lifecycle) view (US-55):
    #   lifecycle: running | paused | done
    #   position:  furthest stage reached (s1..s7)
    #   outcome:   completed | stopped | failed  (null while live)
    #   reason:    why it stopped / the error    (null otherwise)
    # The run's GOAL is `depth` (the human name); its position (archive→s1 …
    # decide→s7) is a 1:1 projection of depth, so it is NOT a separate field —
    # deriving it would just re-introduce a "same fact, two names" pair. `position`
    # (current location) is the only stage field on the surface.
    lifecycle: str | None = None
    position: str | None = None
    outcome: str | None = None
    reason: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _depth_from_store_mode(cls, data):
        # The store dict still uses the legacy "mode" key; surface it as the
        # canonical `depth`. `mode` is no longer returned in the response (US-43
        # deprecation complete) — clients read `depth`. Input still accepts `mode`
        # as an alias (see RunStartRequest/PromoteRequest). The leftover "mode" key
        # here is ignored (RunResponse has no such field).
        #
        # lifecycle/position/outcome/reason are read straight from the row — the
        # store's canonical columns are now authoritative (US-55 step 7d-1).
        if isinstance(data, dict):
            d = dict(data)
            if d.get("depth") is None and d.get("mode") is not None:
                d["depth"] = d["mode"]
            if not d.get("review_url") and d.get("run_id"):
                from config import settings
                if settings.BASE_URL:
                    d["review_url"] = f"{settings.BASE_URL}/runs/{d['run_id']}/review"
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
    from app.models.stages import flatten_claims
    s2 = json.loads(s2_raw["output_json"])["output"]
    # Provenance claims (US-?) replaced the free-text relevance_explanation.
    # Keep the flattened string for back-compat consumers and pass `claims`
    # through so the review surface can show signal/context/inference tags.
    claims = s2.get("claims")
    review: dict = {
        "relevance_score": s2.get("relevance_score"),
        "suggested_depth": normalize_mode(s2.get("suggested_mode")),
        "reasoning": s2.get("suggestion_reasoning"),
        "what_changed": s2.get("what_changed"),
        "reframing": s2.get("reframing"),
        "relevance_explanation": s2.get("relevance_explanation") or flatten_claims(claims),
        "claims": claims,
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

    return await dispatch_start(
        signal_id=body.signal_id,
        signal=signal,
        product_id=body.product_id,
        depth=body.depth,
        force_gate1=body.force_gate1,
        background_tasks=background_tasks,
        engine=engine,
    )


async def dispatch_start(
    signal_id: str,
    signal: dict,
    product_id: str | None,
    depth: str | None,
    force_gate1: bool,
    background_tasks: BackgroundTasks,
    engine: PMEngine,
    origin: str = "start",
) -> RunResponse | BatchStartResponse:
    """Start a run for a signal — manual (product_id given) or fan-out (omitted).

    Shared by ``POST /runs/start`` and ``POST /signals/{id}/refresh`` (the latter
    passes ``origin="refresh"`` so the new run begins a fresh attempt lineage).
    """
    # `depth` (canonical) accepts the `mode` alias; legacy values are normalized (US-43)
    from app.modes import normalize_mode
    requested_mode = normalize_mode(depth)
    if requested_mode is not None:
        _validate_mode(requested_mode)

    if product_id is not None:
        return _start_manual_run(
            signal_id, product_id, requested_mode,
            force_gate1, background_tasks, engine, origin=origin,
        )
    return await _start_fanout_runs(
        signal_id, signal, requested_mode,
        force_gate1, background_tasks, engine, origin=origin,
    )


def _start_manual_run(
    signal_id: str,
    product_id: str,
    requested_mode: str | None,
    force_gate1: bool,
    background_tasks: BackgroundTasks,
    engine: PMEngine,
    origin: str = "start",
) -> RunResponse:
    _validate_product_exists(product_id, engine)
    if requested_mode is not None:
        validate_mode_for_product(requested_mode, product_id)

    run_id = engine.store.create_run(
        product_id=product_id, signal_id=signal_id, origin=origin
    )
    engine.store.advance(run_id, "s1")
    engine.store.update_signal_status(signal_id, "in_run")
    background_tasks.add_task(
        _execute_s1_s2, run_id, signal_id, product_id, requested_mode, engine,
        force_gate1,
    )
    return RunResponse(**engine.store.get_run(run_id))  # type: ignore[arg-type]


async def _start_fanout_runs(
    signal_id: str,
    signal: dict,
    requested_mode: str | None,
    force_gate1: bool,
    background_tasks: BackgroundTasks,
    engine: PMEngine,
    origin: str = "start",
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

    # Conservative fan-out (US-49 §0): spawn ONLY the primary product — the
    # highest-relevance product (within the most-relevant family). Other relevant
    # products are returned as deferred candidates for human-pull promotion, not
    # run; eager fan-out (a run per relevant product) was measured net-negative in
    # production. Membership is intentionally left OPEN (no close_batch_membership
    # here) so promotions can join this batch later; closing happens on
    # POST /batch/{id}/close, which re-enables the Variant 2 synthesis trigger.
    primary_id = _select_primary(triage)
    runs = _spawn_runs_in_batch(
        [primary_id] if primary_id else [],
        signal_id, batch_id, requested_mode, force_gate1, background_tasks, engine,
        origin=origin,
    )
    if runs:
        engine.store.update_signal_status(signal_id, "in_run")

    deferred = [
        p.product_id
        for p in triage.products
        if p.relevant and p.product_id != primary_id
    ]
    # Keep membership OPEN only when there is something to promote. If Triage found
    # no other relevant product, there is provably nothing to defer, so close the
    # batch now — otherwise it would linger open forever (nothing ever triggers the
    # close), accumulating dead single-run batches. Closing also matches the legacy
    # "no other relevant product -> closed batch" behaviour.
    if not deferred:
        engine.store.close_batch_membership(batch_id)

    emit_event(
        "run", "fanout_started", signal_id,
        {"batch_id": batch_id, "primary": primary_id, "deferred": deferred},
    )
    return BatchStartResponse(
        batch_id=batch_id,
        runs=runs,
        triage=[_triage_dict_with_family(p) for p in triage.products],
    )


def _select_primary(triage) -> str | None:
    """The fan-out primary: highest-relevance product among the relevant set.

    Returns None when Triage found nothing relevant (no run is spawned). ``max``
    is stable, so ties break by Triage's product order.
    """
    relevant = [p for p in triage.products if p.relevant]
    if not relevant:
        return None
    return max(relevant, key=lambda p: p.relevance_score).product_id


def _triage_dict_with_family(product_relevance) -> dict:
    """Triage verdict enriched with its product family, so ops can group the
    deferred candidates it offers for promotion ("also relevant, same family")."""
    from config import family_of

    d = product_relevance.model_dump()
    d["family"] = family_of(product_relevance.product_id)
    return d


def _inherited_depth(siblings: list[dict]) -> str | None:
    """The depth a promotion inherits when none is stated: the primary run's
    chosen depth (the first sibling with a depth set), else None (the promoted
    run then takes its own Gate 1 — there is no depth to carry yet)."""
    for r in siblings:
        if r.get("mode"):
            return r["mode"]
    return None


def _spawn_runs_in_batch(
    product_ids: list[str],
    signal_id: str,
    batch_id: str,
    requested_mode: str | None,
    force_gate1: bool,
    background_tasks: BackgroundTasks,
    engine: PMEngine,
    origin: str = "start",
) -> list[RunResponse]:
    """Create one run per product in the batch and start each pipeline."""
    runs: list[RunResponse] = []
    for product_id in product_ids:
        run_id = engine.store.create_run(
            product_id=product_id, signal_id=signal_id, batch_id=batch_id, origin=origin
        )
        engine.store.advance(run_id, "s1")
        background_tasks.add_task(
            _execute_s1_s2, run_id, signal_id, product_id, requested_mode, engine,
            force_gate1,
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


@router.post("/batch/{batch_id}/promote", response_model=None, status_code=202)
async def promote_product(
    batch_id: str,
    body: PromoteRequest,
    background_tasks: BackgroundTasks,
    engine: PMEngine = Depends(get_engine),
) -> RunResponse:
    """Promote a deferred candidate into a sibling run (US-49 §0, human-pull).

    The promoted run re-enters mid-pipeline: **not** at Gate 0 (the signal is
    already admitted) and **not** at Gate 2 (Gate 2 approves *that product's* S4
    output, which does not exist yet). It starts at **S2** with the promoted
    product's context — S1 is product-agnostic and free to re-run (no LLM call),
    so it is effectively reused. Gate 1 is skipped: the promotion carries the
    depth (explicit ``depth``, else the primary's depth).
    """
    from app.modes import normalize_mode
    from app.logging import emit_event

    batch = engine.store.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    if batch["membership_closed"]:
        raise HTTPException(
            status_code=409, detail="Batch membership is closed; cannot promote"
        )
    _validate_product_exists(body.product_id, engine)

    siblings = engine.store.list_runs(batch_id=batch_id, limit=1000)
    if any(r["product_id"] == body.product_id for r in siblings):
        raise HTTPException(
            status_code=409,
            detail=f"Product '{body.product_id}' is already in this batch",
        )

    depth = normalize_mode(body.depth) if body.depth else _inherited_depth(siblings)
    if depth is not None:
        _validate_mode(depth)
        validate_mode_for_product(depth, body.product_id)

    signal_id = batch["signal_id"]
    run_id = engine.store.create_run(
        product_id=body.product_id, signal_id=signal_id, batch_id=batch_id
    )
    engine.store.advance(run_id, "s1")
    engine.store.update_signal_status(signal_id, "in_run")
    background_tasks.add_task(
        _execute_s1_s2, run_id, signal_id, body.product_id, depth, engine
    )
    emit_event(
        "run", "promoted", run_id,
        {"batch_id": batch_id, "product_id": body.product_id, "depth": depth},
    )
    return RunResponse(**engine.store.get_run(run_id))  # type: ignore[arg-type]


@router.post("/batch/{batch_id}/close", response_model=None)
async def close_batch(
    batch_id: str,
    engine: PMEngine = Depends(get_engine),
) -> dict:
    """Close a batch for further promotion (US-49 §0).

    Idempotent. Closing re-enables the Variant 2 synthesis trigger; if every run
    already settled before the close, the finalizer's trigger never fired for the
    now-closed batch, so re-check it here.
    """
    from app.logging import emit_event
    from app.services.portfolio_synthesis import (
        batch_ready_for_synthesis,
        synthesize_batch,
    )

    batch = engine.store.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")

    engine.store.close_batch_membership(batch_id)
    emit_event("run", "batch_closed", batch_id, {"signal_id": batch["signal_id"]})

    if batch_ready_for_synthesis(batch_id, engine):
        await synthesize_batch(batch_id, engine)

    return await get_batch(batch_id, engine)


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

    Note (US-49 §0): this is the bulk "fan to all relevant" action and still spawns
    a run per relevant product. For granular human-pull, prefer per-product
    promotion (POST /runs/batch/{id}/promote), which adds one sibling at a time.

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
        False, background_tasks, engine,
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
    lifecycle: str | None = None,
    position: str | None = None,
    outcome: str | None = None,
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
            "direction", "confirm", "override", "deepen",
        }
        if event not in valid_events:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid event '{event}'. Must be one of: {', '.join(sorted(valid_events))}",
            )
    runs = engine.store.list_runs(
        product_id=product_id,
        lifecycle=lifecycle,
        position=position,
        outcome=outcome,
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
    if not (run.get("lifecycle") == "done" and run.get("outcome") == "completed"):
        raise HTTPException(
            status_code=409,
            detail=f"Run is '{run.get('lifecycle')}/{run.get('outcome')}', "
                   "expected a completed run "
                   "(already reopened runs cannot be reopened again)",
        )

    engine.store.record_approval(run_id=run_id, stage="s2", action="reopen")
    # Revive to Gate 1 (paused@s2) with no depth and no terminal timestamp.
    engine.store.pause(run_id, "s2")
    engine.store.update_run(run_id, mode=None, completed_at=None)
    engine.store.update_signal_status(run["signal_id"], "in_run")
    emit_event("run", "reopened", run_id, {"from": "auto_triaged"})

    updated = engine.store.get_run(run_id)
    return RunResponse(**updated, gate3_review=None)


def _validate_mode(mode: str) -> None:
    from app import pipeline
    valid = set(pipeline.depths())  # single source of truth (app/pipeline.py)
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
    force_gate1: bool = False,
) -> None:
    """Run Stage 1 + Stage 2, then either pause for direction or continue immediately.

    ``force_gate1`` (PM-initiated interactive starts) suppresses S2 auto-triage:
    a no-depth run always pauses at Gate 1 instead of auto-archiving on low
    relevance, so the PM always makes the direction decision. Defaults False so the
    auto-triage path (used by any non-interactive caller) is unchanged.
    """
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

        engine.store.advance(run_id, "s1")
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

        engine.store.advance(run_id, "s2")
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
        elif not force_gate1 and s2_out.output.relevance_score < _cfg.AUTO_TRIAGE_THRESHOLD:
            # No depth specified, not force_gate1, and relevance is below threshold —
            # auto-triage. (force_gate1 from a PM-initiated interactive start
            # suppresses this so the run always pauses at Gate 1 below.)
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
            engine.store.pause(run_id, "s2")
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
        engine.store.advance(run_id, "s2", mode=chosen_mode)
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
    """Advance a run from S2 to its chosen depth's target, pausing at any gate.

    The stage sequence and pause points come from the registry-driven planner
    (``app.runner.plan_advance``) — this function only executes the plan. Stored
    S3/S4 outputs are reused (a deepened run pays only for the stages its new depth
    adds); ``note`` completes at S2 (its insight memo is the artifact — no S7).
    """
    from app import runner
    from app.runner import plan_advance, target_for_depth
    from app.services.run_finalizer import finalize_run

    try:
        s2_raw = engine.store.get_stage_output(run_id, "s2")
        if s2_raw is None:
            raise ValueError("S2 output not found")

        plan = plan_advance("s2", target_for_depth(mode))
        for position in plan.run:
            await runner.run_stage(position, run_id, engine, context)

        if plan.then == "complete":
            await finalize_run(run_id, "completed", engine, event_detail={"mode": mode})
            return

        # Segment 1 only ever pauses at S4 (Gate 2, decide mode).
        await _pause_at_gate2(run_id, context, engine, s2_raw)

    except Exception as exc:  # noqa: BLE001
        await finalize_run(run_id, "failed", engine, event_detail={"error": str(exc)})


async def _pause_at_gate2(run_id: str, context, engine: PMEngine, s2_raw: dict) -> None:
    """Pause a decide run at Gate 2 (post-S4 human approval) and notify the PM."""
    import json as _json

    from app.logging import emit_event
    from app.models.stages import S4OutputData
    from config import settings as _notify_cfg

    s4_raw = engine.store.get_stage_output(run_id, "s4")
    s4_output_data = S4OutputData(**_json.loads(s4_raw["output_json"])["output"])

    engine.store.pause(run_id, "s4")
    emit_event("run", "waiting_approval", run_id)

    signal_for_notify = engine.store.get_signal(
        _json.loads(s2_raw["output_json"]).get("signal_id", "")
    )
    signal_title = signal_for_notify["title"] if signal_for_notify else run_id
    personas = {p.persona: p for p in s4_output_data.personas}
    skeptic_concern = personas["skeptic"].key_argument if "skeptic" in personas else ""
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
