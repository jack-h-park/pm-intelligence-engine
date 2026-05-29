"""run_finalizer — single exit point for all terminal run transitions.

Every path that completes, kills, or fails a run must call finalize_run().
This guarantees:
  - completed_at is stamped for completed/killed runs (via the store layer)
  - decision-system export fires for eligible runs (decide mode only)
  - terminal events are emitted uniformly

Export policy
-------------
Only 'decide' mode runs are exported to DECISION_SYSTEM_ROOT.
Modes file/brief/opportunity/evaluate produce no export artifact.
Auto-triaged runs (completed as mode='file') are also excluded.

Wiki sync is NOT owned by pm-platform. Hermes consumes terminal run events
and performs wiki sync independently. See EXPORT_AND_SYNC_CONTRACT.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.factory import PMEngine

# Only decide-mode runs export to the decision-system.
_EXPORTABLE_MODES = {"decide"}
_TERMINAL_SIGNAL_STATUSES = {
    "completed": "done",
    "killed": "done",
    "failed": "pending",
}


def finalize_run(
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
        Arbitrary extra payload merged into the event (e.g. ``{"mode": "brief"}``).
    """
    from app.logging import emit_event

    # Apply terminal state (store layer handles completed_at stamping).
    engine.store.update_run(run_id, status=status, current_stage=None)
    _sync_signal_status(run_id, status, engine)

    action = event_action if event_action is not None else status
    emit_event("run", action, run_id, event_detail or {})

    # Export to decision-system for eligible completed runs.
    if status == "completed":
        _maybe_export(run_id, engine)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


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
        emit_event("run_exporter", "exported", run_id, {"path": str(path)})
    except OSError:
        # decision_system_root not mounted — non-fatal.
        emit_event("run_exporter", "export_skipped", run_id, {
            "reason": "decision_system_root not writable",
            "decision_system_root": settings.DECISION_SYSTEM_ROOT,
        })


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
