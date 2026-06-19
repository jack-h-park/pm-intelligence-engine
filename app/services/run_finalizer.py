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

Wiki sync is NOT owned by pm-engine. Hermes consumes terminal run events
and performs wiki sync independently. See EXPORT_AND_SYNC_CONTRACT.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from app.factory import PMEngine

# note depth and above export to the run archive; archive depth (set-aside) is excluded.
_EXPORTABLE_MODES = {"note", "structure", "evaluate", "decide"}
_TERMINAL_SIGNAL_STATUSES = {
    "completed": "done",
    "killed": "done",
    "failed": "new",  # failed run returns the signal to the retryable pool (was "pending")
}


async def finalize_run(
    run_id: str,
    status: str,
    engine: PMEngine,
    event_action: str | None = None,
    event_detail: dict | None = None,
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
        semantic precision — the generic terminal event is still useful to Hermes
        for polling but the specific action label preserves intent.
    event_detail:
        Arbitrary extra payload merged into the event (e.g. ``{"mode": "note"}``).
    """
    from app.logging import emit_event

    # Apply terminal state (store layer handles completed_at stamping). Roll up
    # per-stage token usage into run-level totals at the same time (Phase 2).
    token_totals = _sum_run_tokens(run_id, engine)
    engine.store.update_run(run_id, status=status, current_stage=None, **token_totals)
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


def _sum_run_tokens(run_id: str, engine: PMEngine) -> dict:
    """Sum per-stage token usage from stage_outputs metadata (Phase 2).

    Reads every stage output's ``metadata.input_tokens`` / ``output_tokens`` and
    sums them — all versions included, since each version's tokens were really
    spent (a revise re-runs S4, etc.). Returns an empty dict when no stage recorded
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
    """Keep the source signal lifecycle aligned with terminal run outcomes."""
    target_status = _TERMINAL_SIGNAL_STATUSES.get(status)
    if target_status is None:
        return

    run = engine.store.get_run(run_id)
    if run is None:
        return

    signal_id = run.get("signal_id")
    if signal_id is None:
        return

    engine.store.update_signal_status(signal_id, target_status)
