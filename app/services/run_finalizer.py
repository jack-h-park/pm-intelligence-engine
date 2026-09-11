"""run_finalizer — single exit point for all terminal run transitions.

Every path that completes, kills, or fails a run must call finalize_run().
This guarantees:
  - completed_at is stamped for completed/killed runs (via the store layer)
  - decision-system export fires for eligible runs (decide mode only)
  - terminal events are emitted uniformly

Export policy
-------------
Every completed run at depth note/structure/evaluate/decide is exported to the
canonical run archive (browsable markdown trace). Only 'archive' depth
(S1-only, set aside / not pursued) is excluded - it produces no artifact.

Wiki sync is NOT owned by pm-engine. The operations plane consumes terminal run events
and performs wiki sync independently. See EXPORT_AND_SYNC_CONTRACT.md.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from app import pipeline

if TYPE_CHECKING:
    from app.factory import PMEngine

# note depth and above export to the run archive; archive depth (set-aside) is excluded.
_EXPORTABLE_MODES = {"note", "structure", "evaluate", "decide"}

# Terminal-reason vocabulary keyed by the semantic event_action a caller passes.
# The remaining reasons (archived/noted/structured/evaluated/decided) have no
# distinguishing event_action — they are derived from the run's depth instead.
_ENDED_BY_FROM_ACTION = {
    "auto_triaged": "auto_triaged",
    "rejected": "rejected",
    "kill_confirmed": "kill_confirmed",
    "kill_overridden": "kill_overridden",
    "voided": "voided",
}
# Depth → completed-reason, for a normal completion (no distinguishing action).
# Derived from the stage registry (single source of truth) rather than restated.
_ENDED_BY_FROM_MODE = pipeline.ended_by_by_depth()


def _derive_ended_by(status: str, event_action: str | None, mode: str | None) -> str:
    """Collapse (status, event_action, mode) into one terminal-reason token.

    Precedence: failure first, then a semantic action (which distinguishes a PM
    Gate-1 ``archive`` from an S2 ``auto_triaged`` — both leave ``mode=archive``),
    then the depth a plain completion ran to. Falls back to the bare status so the
    column is never NULL for a run this function stamps.
    """
    if status == "failed":
        return "failed"
    if event_action in _ENDED_BY_FROM_ACTION:
        return _ENDED_BY_FROM_ACTION[event_action]
    if status == "completed" and mode in _ENDED_BY_FROM_MODE:
        return _ENDED_BY_FROM_MODE[mode]
    return status


async def finalize_run(
    run_id: str,
    status: str,
    engine: PMEngine,
    event_action: str | None = None,
    event_detail: dict[str, Any] | None = None,
) -> None:
    """Apply a terminal status to a run and trigger associated side effects.

    Parameters
    ----------
    run_id:
        The run to finalize.
    status:
        Terminal status — ``"completed"``, ``"killed"``, or ``"failed"``.
        The store auto-stamps ``completed_at`` for completed/killed runs.
        Failed runs intentionally do NOT receive ``completed_at``.
    engine:
        PMEngine providing store, llm, and notifier access.
    event_action:
        Action label for ``emit_event``. Defaults to *status*.
        Pass ``"kill_confirmed"``, ``"rejected"``, ``"auto_triaged"`` etc. for
        semantic precision — the generic terminal event is still useful to the ops plane
        for polling but the specific action label preserves intent.
    event_detail:
        Arbitrary extra payload merged into the event (e.g. ``{"mode": "note"}``).
    """
    from app.logging import emit_event

    # Read the live row once — for the failure stage (before we clear it) and for
    # the depth the ended_by derivation needs.
    run_before = engine.store.get_run(run_id)
    failure_fields: dict[str, Any] = {}
    if status == "failed":
        failure_fields = {
            # The live position is the stage that was executing when it failed
            # (US-55: `position` replaced the retired `current_stage`).
            "failed_stage": (run_before or {}).get("position"),
            "error": (event_detail or {}).get("error"),
        }

    ended_by = _derive_ended_by(status, event_action, (run_before or {}).get("mode"))

    # Translate the terminal status into the authoritative (outcome, position, reason)
    # model — the single source of truth (US-55 step 7d-3 dropped the legacy
    # ended_by/failed_stage/error columns; their information now lives in position and
    # reason). position is the target for a completion, the failed stage for a failure,
    # none for a kill; reason is the failure error, the kill's stop-kind, or none.
    _mode = (run_before or {}).get("mode")
    outcome = {"completed": "completed", "killed": "stopped", "failed": "failed"}[status]
    if status == "completed":
        position = pipeline.position_for_depth(_mode) if _mode else None
        reason = None
    elif status == "failed":
        position = failure_fields.get("failed_stage")   # the stage that was executing
        reason = failure_fields.get("error")            # the exception string
    else:  # killed
        position = None
        reason = ended_by                                # the stop-kind (rejected/…)

    # Roll up per-stage token usage into run-level totals at the same time (Phase 2).
    token_totals = _sum_run_tokens(run_id, engine)
    engine.store.finish(
        run_id, outcome, position=position, reason=reason, **token_totals,
    )
    _sync_signal_status(run_id, status, engine)

    action = event_action if event_action is not None else status
    emit_event("run", action, run_id, event_detail or {})

    # Export to decision-system for eligible completed runs.
    if status == "completed":
        _maybe_export(run_id, engine)

    # Portfolio synthesis (US-49, Variant 2): if this run was the last to settle
    # in a fan-out batch, produce the cross-product memo. Fires on any terminal
    # status — a killed/failed sibling can be the one that completes the batch.
    await _maybe_synthesize_portfolio(run_id, engine)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sum_run_tokens(run_id: str, engine: PMEngine) -> dict[str, Any]:
    """Sum per-stage token usage from stage_outputs metadata (Phase 2).

    Reads every stage output's ``metadata.input_tokens`` / ``output_tokens`` and
    sums them — all versions included, since each version's tokens were really
    spent (a revise re-runs S4, etc.). Returns an empty dict[str, Any] when no stage recorded
    tokens, so older runs / no-LLM runs leave the columns NULL.
    """
    import json

    inp = out = 0
    seen = False
    for so in engine.store.get_all_stage_outputs(run_id):
        raw = so.get("output_json")
        if not raw:
            continue
        try:
            meta = json.loads(raw).get("metadata", {})
        except (ValueError, TypeError):
            continue
        it, ot = meta.get("input_tokens"), meta.get("output_tokens")
        if it is not None:
            inp += it
            seen = True
        if ot is not None:
            out += ot
            seen = True
    if not seen:
        return {}
    return {"prompt_tokens_total": inp, "completion_tokens_total": out}


def _maybe_export(run_id: str, engine: PMEngine) -> None:
    """Export run artifacts to DECISION_SYSTEM_ROOT when policy allows."""
    from app.logging import emit_event
    from app.services.run_exporter import export_run
    from config import settings

    run = engine.store.get_run(run_id)
    if run is None or run.get("mode") not in _EXPORTABLE_MODES:
        return

    try:
        path = export_run(
            run_id=run_id,
            store=engine.store,
            decision_system_root=settings.DECISION_SYSTEM_ROOT,
        )
        emit_event("run_exporter", "exported", run_id, {
            "path": str(path),
            "canonical_path": str(path),
        })
    except OSError:
        # decision_system_root not mounted — non-fatal.
        emit_event("run_exporter", "export_skipped", run_id, {
            "reason": "decision_system_root not writable",
            "decision_system_root": settings.DECISION_SYSTEM_ROOT,
        })


async def _maybe_synthesize_portfolio(run_id: str, engine: PMEngine) -> None:
    """Trigger Variant 2 synthesis when the run's batch is complete.

    Cheap guards run first (no batch / membership still open / already
    synthesized / single run / a sibling still unsettled); only a genuinely
    complete batch reaches the LLM call. The store's insert-or-skip guard makes a
    near-simultaneous double-fire harmless.
    """
    run = engine.store.get_run(run_id)
    if run is None or not run.get("batch_id"):
        return

    from app.services.portfolio_synthesis import (
        batch_ready_for_synthesis,
        synthesize_batch,
    )

    batch_id = run["batch_id"]
    if not batch_ready_for_synthesis(batch_id, engine):
        return
    await synthesize_batch(batch_id, engine)


def _sync_signal_status(run_id: str, status: str, engine: PMEngine) -> None:
    """Re-derive the source signal's lifecycle status from *all* of its runs.

    The signal status is a deterministic function of its runs (see
    ``app.services.signal_status``), not a value pushed from the single finalizing
    run — that older approach diverged whenever a run was created outside this
    path (synthetic/backfilled ``completed`` rows) or a fan-out left siblings in
    different states. Reconciling from the full run set is correct in both cases.

    The retry-exhaustion *event* still fires here: it is about *this* run's
    lineage hitting the cap, which the status derivation (a set-level view)
    cannot express on its own.
    """
    run = engine.store.get_run(run_id)
    if run is None:
        return

    signal_id = run.get("signal_id")
    if signal_id is None:
        return

    if status == "failed":
        from config import settings

        attempt_no = run.get("attempt_no") or 1
        if attempt_no >= settings.MAX_RUN_ATTEMPTS:
            from app.logging import emit_event

            emit_event(
                "run",
                "retry_exhausted",
                run_id,
                {"attempt_no": attempt_no, "max_attempts": settings.MAX_RUN_ATTEMPTS},
            )

    from app.services.signal_status import reconcile_signal_status

    reconcile_signal_status(signal_id, engine)
