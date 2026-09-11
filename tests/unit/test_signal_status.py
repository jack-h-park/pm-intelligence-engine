"""Unit tests for app.services.signal_status — deterministic derivation of
``signals.status`` from a signal's runs, plus the reconciliation helpers.

Covers the rules in ``derive_signal_status`` and the regression that motivated
this module: a signal holding a ``completed`` run that bypassed ``finalize_run``
(synthetic/backfilled row) staying stuck at ``in_run`` instead of ``done``.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.services.signal_status import (
    derive_signal_status,
    reconcile_all_signals,
    reconcile_signal_status,
)

MAX = 3


# ---------------------------------------------------------------------------
# derive_signal_status — the pure rules
# ---------------------------------------------------------------------------


# Canonical run-state shorthands (US-55): a run is described by (lifecycle,
# outcome), not the retired `status` string.
_RUNNING = {"lifecycle": "running", "outcome": None}
_PAUSED = {"lifecycle": "paused", "outcome": None}


def _done(outcome: str, attempt_no: int = 1) -> dict[str, Any]:
    return {"lifecycle": "done", "outcome": outcome, "attempt_no": attempt_no}


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        # no runs → never started
        ([], "new"),
        # any non-terminal run → in_run
        ([_RUNNING], "in_run"),
        ([_PAUSED], "in_run"),
        # a resolved sibling alongside an unsettled one is still in_run
        ([_done("completed"), _RUNNING], "in_run"),
        # all terminal with a resolved outcome → done
        ([_done("completed")], "done"),
        ([_done("stopped")], "done"),
        # completed wins over a failed sibling
        ([_done("completed"), _done("failed")], "done"),
        # all failed, below the cap → retryable new
        ([_done("failed", 1)], "new"),
        ([_done("failed", 2)], "new"),
        # all failed, a lineage hit the cap → blocked
        ([_done("failed", 3)], "blocked"),
        ([_done("failed", 1), _done("failed", 3)], "blocked"),
    ],
)
def test_derive_signal_status(runs, expected):
    assert derive_signal_status(runs, MAX) == expected


def test_derive_missing_attempt_no_defaults_to_one():
    """A failed run with no attempt_no is treated as attempt 1 (below the cap)."""
    assert derive_signal_status(
        [{"lifecycle": "done", "outcome": "failed"}], MAX
    ) == "new"


# ---------------------------------------------------------------------------
# reconcile_signal_status — recompute + persist one signal
# ---------------------------------------------------------------------------


def _engine_with(signal_status, runs, max_attempts=MAX):
    engine = MagicMock()
    engine.store.get_signal.return_value = {"signal_id": "sig-1", "status": signal_status}
    engine.store.list_runs.return_value = runs
    # reconcile reads config.settings.MAX_RUN_ATTEMPTS at call time
    import config

    config.settings.MAX_RUN_ATTEMPTS = max_attempts
    return engine


def test_reconcile_fixes_stuck_in_run():
    """The Glasswing regression: a completed run left the signal at in_run."""
    engine = _engine_with("in_run", [_done("completed")])
    result = reconcile_signal_status("sig-1", engine)
    assert result == "done"
    engine.store.update_signal_status.assert_called_once_with("sig-1", "done")


def test_reconcile_noop_when_already_correct():
    """No write (and None returned) when the stored status already matches."""
    engine = _engine_with("done", [_done("completed")])
    result = reconcile_signal_status("sig-1", engine)
    assert result is None
    engine.store.update_signal_status.assert_not_called()


def test_reconcile_missing_signal_is_noop():
    engine = MagicMock()
    engine.store.get_signal.return_value = None
    assert reconcile_signal_status("ghost", engine) is None
    engine.store.update_signal_status.assert_not_called()


# ---------------------------------------------------------------------------
# reconcile_all_signals — table sweep
# ---------------------------------------------------------------------------


def test_reconcile_all_reports_only_corrections():
    engine = MagicMock()
    import config

    config.settings.MAX_RUN_ATTEMPTS = MAX
    engine.store.list_signals.return_value = [
        {"signal_id": "a", "title": "Stuck", "status": "in_run"},   # → done
        {"signal_id": "b", "title": "Fine", "status": "done"},      # already correct
    ]
    runs_by_signal = {
        "a": [_done("completed")],
        "b": [_done("completed")],
    }
    engine.store.list_runs.side_effect = lambda signal_id, limit: runs_by_signal[signal_id]

    changes = reconcile_all_signals(engine)

    assert changes == [{"signal_id": "a", "title": "Stuck", "old": "in_run", "new": "done"}]
    engine.store.update_signal_status.assert_called_once_with("a", "done")
