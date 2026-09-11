"""Deepen API — resume a completed run at a deeper processing depth.

The depth ladder (archive < note < structure < evaluate < decide) was a one-shot
choice at Gate 1 — made at the moment of least information. Once a run completed
at note/structure, going deeper meant starting a brand-new run and re-paying
S1–S3. Production data (5 weeks / 17 Gate 1 decisions) showed the predictable
result: the PM systematically picks shallow depths and `decide` is never reached.

`deepen` converts the depth choice from an upfront forecast into an incremental
pull: process shallow first, read the artifact, and deepen if it earns it. The
resumed pipeline reuses stored stage outputs (S1–S4 are immutable per run) and
executes only the stages the new depth adds — the same human-pull principle as
batch promotion (US-49 §0).

Distinct from `reopen` (US-31), which revives an *auto-triaged* run back to
Gate 1 with no depth set. `deepen` acts on any completed run with a depth and
carries the new depth directly — no second Gate 1 pause.

State transition:
  completed (mode=m) + POST /deepen { depth: d > m } → running (stages m..d)
  → then the normal rules for depth d (decide still pauses at Gate 2).
"""

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.api.deps import get_engine
from app.api.runs import validate_mode_for_product
from app.factory import PMEngine
from app.modes import MODES, normalize_mode

router = APIRouter(prefix="/runs", tags=["deepen"])


class DeepenRequest(BaseModel):
    # `depth` is canonical (US-43); `mode` accepted as a deprecated alias.
    model_config = ConfigDict(populate_by_name=True)
    depth: str = Field(validation_alias=AliasChoices("depth", "mode"))


@router.post("/{run_id}/deepen", status_code=202)
async def deepen_run(
    run_id: str,
    body: DeepenRequest,
    background_tasks: BackgroundTasks,
    engine: PMEngine = Depends(get_engine),
) -> dict[str, Any]:
    target = normalize_mode(body.depth)
    if target not in MODES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid depth '{body.depth}'. Must be one of: {', '.join(MODES)}",
        )

    run = engine.store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if not (run.get("lifecycle") == "done" and run.get("outcome") == "completed"):
        raise HTTPException(
            status_code=409,
            detail=f"Run is '{run.get('lifecycle')}/{run.get('outcome')}', "
                   "expected a completed run — only completed runs can be deepened",
        )
    current = run.get("mode")
    if current is None:
        raise HTTPException(
            status_code=409,
            detail="Run has no depth to deepen from (use /reopen for "
                   "auto-triaged runs without a PM direction)",
        )
    if MODES.index(target) <= MODES.index(current):
        raise HTTPException(
            status_code=409,
            detail=f"Target depth '{target}' is not deeper than current '{current}'",
        )
    validate_mode_for_product(target, run["product_id"])

    # The resume path replays from stored S2 output; every completed run with a
    # depth has one by construction, but guard against hand-crafted rows.
    if engine.store.get_stage_output(run_id, "s2") is None:
        raise HTTPException(
            status_code=409,
            detail="Run has no stored S2 output; cannot resume — start a new run",
        )

    # Labeled calibration datapoint, symmetric with Gate 1 `direction`:
    # which depth the PM pulled to after reading the shallow artifact.
    engine.store.record_approval(
        run_id=run_id,
        stage="s2",
        action="deepen",
        feedback_text=f"from={current}; to={target}",
    )
    # Resume the run: back to running at its prior position with the new depth.
    # _continue_after_direction advances position per-stage from here.
    engine.store.advance(run_id, run.get("position") or "s2", mode=target)
    engine.store.update_run(run_id, completed_at=None)
    engine.store.update_signal_status(run["signal_id"], "in_run")

    background_tasks.add_task(_execute_deepen, run_id, target, current, engine)

    return {
        "run_id": run_id,
        "action": "deepen_started",
        "from_depth": current,
        "depth": target,
    }


async def _execute_deepen(
    run_id: str, target: str, from_depth: str, engine: PMEngine
) -> None:
    from app.logging import emit_event
    from app.services.run_finalizer import finalize_run

    try:
        run = engine.store.get_run(run_id)
        if run is None:
            return

        from app.services.run_context import load_run_context
        context = load_run_context(run_id, engine)

        emit_event("run", "deepened", run_id, {"from": from_depth, "to": target})

        # _continue_after_direction reuses stored S3/S4 outputs, so only the
        # stages the new depth adds are executed.
        from app.api.runs import _continue_after_direction
        await _continue_after_direction(run_id, target, context, engine)

    except Exception as exc:  # noqa: BLE001
        await finalize_run(run_id, "failed", engine, event_detail={"error": str(exc)})
