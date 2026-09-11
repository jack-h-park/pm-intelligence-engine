"""Deterministic derivation of ``signals.status`` from a signal's runs.

A signal's lifecycle status (``new`` · ``in_run`` · ``done`` · ``blocked``) is a
*function* of its runs, not a field to be pushed independently. Historically each
terminal run shoved a status onto its signal (the old
``run_finalizer._sync_signal_status`` mapped ``completed/killed -> done``,
``failed -> new`` from the single finalizing run). That diverged whenever:

  - a run was created *outside* ``finalize_run`` — backfill / synthetic
    ``completed`` rows never ran the sync, so a completed run could leave its
    signal stuck at ``in_run`` (the 2026-06-21 "Glasswing" incident); or
  - a fan-out left siblings in different states — one run completing flipped the
    signal to ``done`` while siblings were still mid-pipeline, and one run
    failing flipped it back to ``new`` even though a sibling had completed.

This module makes the status *derivable* (a pure function of all the signal's
runs) and *reconcilable* (recompute-and-persist, idempotent). ``finalize_run``
now reconciles instead of pushing, and the ``POST /signals/reconcile``
maintenance endpoint repairs rows that drifted before this invariant existed.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from app.factory import PMEngine

# completed/stopped are *resolved* outcomes (a real decision or a deliberate
# kill). ``failed`` is terminal-but-retryable — it does not, on its own, mean the
# signal is done. Read from the canonical run-state columns (US-55): a run is
# terminal iff ``lifecycle == "done"``; its ``outcome`` distinguishes the three.
_RESOLVED_OUTCOMES = {"completed", "stopped"}


def _is_terminal(run: dict[str, Any]) -> bool:
    return run.get("lifecycle") == "done"


def derive_signal_status(runs: list[dict[str, Any]], max_attempts: int) -> str:
    """Return the correct ``signals.status`` for a signal with these runs.

    Deterministic and idempotent — depends only on the runs, not on call order:

      - no runs                                   -> ``"new"``   (nothing started)
      - any run not yet terminal                  -> ``"in_run"``
      - all runs terminal, >=1 resolved           -> ``"done"``
      - all runs failed, a lineage hit the cap    -> ``"blocked"``
      - all runs failed, all below the cap         -> ``"new"`` (retryable)

    ``max_attempts`` is ``settings.MAX_RUN_ATTEMPTS`` — a failed lineage that has
    reached it is parked as ``blocked`` (out of the auto-retry pool until a human
    investigates) rather than returned to the retryable ``new`` pool.
    """
    if not runs:
        return "new"
    if any(not _is_terminal(r) for r in runs):
        return "in_run"
    if any(r.get("outcome") in _RESOLVED_OUTCOMES for r in runs):
        return "done"
    # Every run is terminal and all of them failed.
    if any((r.get("attempt_no") or 1) >= max_attempts for r in runs):
        return "blocked"
    return "new"


def reconcile_signal_status(signal_id: str, engine: PMEngine) -> str | None:
    """Recompute one signal's status from its runs and persist any change.

    Returns the new status if it changed, else ``None``. Safe to call repeatedly
    (idempotent) and safe to call after any run transition — it reads *all* the
    signal's runs, so it is correct regardless of which run triggered it.
    """
    from config import settings

    signal = engine.store.get_signal(signal_id)
    if signal is None:
        return None

    # limit is a generous ceiling: a single signal's fan-out + retries never
    # approaches it, but we must not silently truncate the run set we derive from.
    runs = engine.store.list_runs(signal_id=signal_id, limit=1000)
    target = derive_signal_status(runs, settings.MAX_RUN_ATTEMPTS)

    if target != signal.get("status"):
        engine.store.update_signal_status(signal_id, target)
        return target
    return None


def reconcile_all_signals(engine: PMEngine) -> list[dict[str, Any]]:
    """Sweep every signal, repairing any whose status diverges from its runs.

    Returns one entry ``{signal_id, title, old, new}`` per *corrected* signal
    (unchanged signals are omitted). Used by ``POST /signals/reconcile`` to fix
    rows that drifted before this invariant existed — e.g. a signal left at
    ``in_run`` by a synthetic/backfilled ``completed`` run that bypassed
    ``finalize_run`` — without hand-editing the database.
    """
    from config import settings

    changes: list[dict[str, Any]] = []
    # Large ceiling: repair must cover the whole table, not the default page.
    for signal in engine.store.list_signals(limit=100000):
        signal_id = signal["signal_id"]
        runs = engine.store.list_runs(signal_id=signal_id, limit=1000)
        target = derive_signal_status(runs, settings.MAX_RUN_ATTEMPTS)
        old = signal.get("status")
        if target != old:
            engine.store.update_signal_status(signal_id, target)
            changes.append(
                {
                    "signal_id": signal_id,
                    "title": signal.get("title"),
                    "old": old,
                    "new": target,
                }
            )
    return changes
