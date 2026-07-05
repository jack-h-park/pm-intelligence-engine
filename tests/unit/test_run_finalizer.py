"""Unit tests for run_finalizer — single exit point for terminal run transitions.

Covers:
  - await finalize_run() calls store.finish with the right (outcome, position, reason)
  - completed_at is stamped by the store for completed/killed (store-layer contract)
  - export is triggered for completed runs at exportable depth (note and above)
  - export is NOT triggered for completed 'archive' depth (set-aside)
  - export is NOT triggered for killed or failed
  - event_action defaults to status when not provided
  - custom event_action is passed through
"""

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine(mode: str = "decide", status: str = "running") -> MagicMock:
    """Build a minimal mock PMEngine whose store returns a sensible run dict."""
    engine = MagicMock()
    engine.store.get_run.return_value = {
        "run_id": "run-abc",
        "product_id": "example-security-product",
        "signal_id": "signal-abc",
        "mode": mode,
        "status": status,
        "routing": "prd",
    }
    engine.store.update_run = MagicMock()
    return engine


# ---------------------------------------------------------------------------
# Basic store contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_run_calls_store_finish_with_state():
    """finalize_run must call store.finish with the terminal (outcome, position, reason)."""
    from app.services.run_finalizer import finalize_run

    engine = _make_engine()
    with patch("app.services.run_finalizer._maybe_export"):
        with patch("app.logging.emit_event"):
            await finalize_run("run-abc", "completed", engine)

    # completed + mode=decide -> outcome=completed, position=s7 (target), ended_by=decided.
    engine.store.finish.assert_called_once_with(
        "run-abc", "completed", position="s7", reason=None, ended_by="decided"
    )


@pytest.mark.asyncio
async def test_finalize_run_killed_calls_store_finish():
    """finalize_run with 'killed' -> store.finish(outcome='stopped')."""
    from app.services.run_finalizer import finalize_run

    engine = _make_engine()
    with patch("app.logging.emit_event"):
        await finalize_run("run-abc", "killed", engine)

    # killed -> outcome=stopped, position=None, reason=ended_by ("killed" fallback).
    engine.store.finish.assert_called_once_with(
        "run-abc", "stopped", position=None, reason="killed", ended_by="killed"
    )


@pytest.mark.asyncio
async def test_finalize_run_failed_calls_store_finish():
    """finalize_run with 'failed' records failed_stage + error alongside the state."""
    from app.services.run_finalizer import finalize_run

    engine = _make_engine()
    with patch("app.logging.emit_event"):
        await finalize_run("run-abc", "failed", engine)

    # No current_stage on the mock run and no error in event_detail -> both None.
    engine.store.finish.assert_called_once_with(
        "run-abc", "failed", position=None, reason=None, ended_by="failed",
        failed_stage=None, error=None,
    )


@pytest.mark.asyncio
async def test_finalize_run_failed_persists_stage_and_error():
    """The executing stage and the exception string are persisted on failure."""
    from app.services.run_finalizer import finalize_run

    engine = _make_engine()
    engine.store.get_run.return_value = {
        "run_id": "run-abc",
        "signal_id": "signal-abc",
        "position": "s2",
        "attempt_no": 1,
    }
    with patch("app.logging.emit_event"):
        await finalize_run(
            "run-abc", "failed", engine, event_detail={"error": "boom 400"}
        )

    # failed -> position = failed stage (s2), reason = error.
    engine.store.finish.assert_called_once_with(
        "run-abc", "failed", position="s2", reason="boom 400", ended_by="failed",
        failed_stage="s2", error="boom 400",
    )


@pytest.mark.asyncio
async def test_finalize_run_failed_blocks_signal_after_max_attempts():
    """A failure on the final allowed attempt parks the signal as 'blocked'."""
    from app.services.run_finalizer import finalize_run

    engine = _make_engine()
    engine.store.get_run.return_value = {
        "run_id": "run-abc",
        "signal_id": "signal-abc",
        "current_stage": "s1",
        "attempt_no": 3,  # == default MAX_RUN_ATTEMPTS
    }
    # Signal status now derives from ALL the signal's runs: the lone run failed on
    # its final attempt, so the signal is parked as 'blocked'.
    engine.store.get_signal.return_value = {"signal_id": "signal-abc", "status": "in_run"}
    engine.store.list_runs.return_value = [
        {"lifecycle": "done", "outcome": "failed", "attempt_no": 3}
    ]
    with patch("config.settings") as mock_settings:
        mock_settings.MAX_RUN_ATTEMPTS = 3
        with patch("app.logging.emit_event") as mock_emit:
            await finalize_run("run-abc", "failed", engine)

    engine.store.update_signal_status.assert_called_once_with("signal-abc", "blocked")
    assert any(c.args[1] == "retry_exhausted" for c in mock_emit.call_args_list)


@pytest.mark.asyncio
async def test_finalize_run_failed_retries_below_max():
    """A failure below the cap returns the signal to the retryable 'new' pool."""
    from app.services.run_finalizer import finalize_run

    engine = _make_engine()
    engine.store.get_run.return_value = {
        "run_id": "run-abc",
        "signal_id": "signal-abc",
        "current_stage": "s1",
        "attempt_no": 1,
    }
    # Lone run failed below the cap → signal returns to the retryable 'new' pool.
    engine.store.get_signal.return_value = {"signal_id": "signal-abc", "status": "in_run"}
    engine.store.list_runs.return_value = [
        {"lifecycle": "done", "outcome": "failed", "attempt_no": 1}
    ]
    with patch("config.settings") as mock_settings:
        mock_settings.MAX_RUN_ATTEMPTS = 3
        with patch("app.logging.emit_event"):
            await finalize_run("run-abc", "failed", engine)

    engine.store.update_signal_status.assert_called_once_with("signal-abc", "new")


@pytest.mark.parametrize(
    ("status", "expected_signal_status"),
    [
        ("completed", "done"),
        ("killed", "done"),
        ("failed", "new"),
    ],
)
@pytest.mark.asyncio
async def test_finalize_run_updates_signal_status(status: str, expected_signal_status: str):
    """finalize_run re-derives Signal.status from the signal's runs.

    With a single run carrying the just-finalized outcome: completed/killed →
    done; a sole failed run (attempt 1, below the cap) → retryable new.
    """
    from app.services.run_finalizer import finalize_run

    engine = _make_engine()
    engine.store.get_signal.return_value = {"signal_id": "signal-abc", "status": "in_run"}
    # The reconcile reads the canonical (lifecycle, outcome) of the signal's runs.
    _outcome = {"completed": "completed", "killed": "stopped", "failed": "failed"}[status]
    engine.store.list_runs.return_value = [
        {"lifecycle": "done", "outcome": _outcome, "attempt_no": 1}
    ]
    with patch("app.logging.emit_event"):
        with patch("app.services.run_finalizer._maybe_export"):
            await finalize_run("run-abc", status, engine)

    engine.store.update_signal_status.assert_called_once_with(
        "signal-abc",
        expected_signal_status,
    )


@pytest.mark.asyncio
async def test_finalize_run_skips_signal_status_update_when_run_not_found():
    """Signal status update is skipped when the run cannot be reloaded."""
    from app.services.run_finalizer import finalize_run

    engine = _make_engine()
    engine.store.get_run.return_value = None

    with patch("app.logging.emit_event"):
        await finalize_run("run-abc", "failed", engine)

    engine.store.update_signal_status.assert_not_called()


# ---------------------------------------------------------------------------
# Export policy — completed runs at note depth and above trigger export
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_triggered_for_completed_decide():
    """Export fires when status=completed and mode=decide."""
    from app.services.run_finalizer import finalize_run

    engine = _make_engine(mode="decide")
    with patch("app.services.run_finalizer._maybe_export") as mock_export:
        with patch("app.logging.emit_event"):
            await finalize_run("run-abc", "completed", engine)

    mock_export.assert_called_once_with("run-abc", engine)


@pytest.mark.parametrize("mode", ["archive", "note", "structure", "evaluate", "decide"])
@pytest.mark.asyncio
async def test_maybe_export_invoked_for_all_completed_runs(mode: str):
    """finalize_run delegates to _maybe_export for every completed run,
    regardless of depth; the depth guard lives inside _maybe_export."""
    from app.services.run_finalizer import finalize_run

    engine = _make_engine(mode=mode)
    with patch("app.services.run_finalizer._maybe_export") as mock_export:
        with patch("app.logging.emit_event"):
            await finalize_run("run-abc", "completed", engine)

    # The depth guard lives inside _maybe_export (tested below); here we only
    # confirm the completed path always delegates to it.
    mock_export.assert_called_once()


@pytest.mark.parametrize("status", ["killed", "failed"])
@pytest.mark.asyncio
async def test_export_not_triggered_for_non_completed_statuses(status: str):
    """Export must NOT fire for killed or failed regardless of mode."""
    from app.services.run_finalizer import finalize_run

    engine = _make_engine(mode="decide")
    with patch("app.services.run_finalizer._maybe_export") as mock_export:
        with patch("app.logging.emit_event"):
            await finalize_run("run-abc", status, engine)

    mock_export.assert_not_called()


# ---------------------------------------------------------------------------
# _maybe_export mode guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_export_skips_when_mode_not_exportable():
    """_maybe_export must return early when run.mode is not in _EXPORTABLE_MODES."""
    from app.services.run_finalizer import _maybe_export

    engine = _make_engine(mode="archive")
    # export_run is lazily imported inside _maybe_export; patch at its source module
    with patch("app.services.run_exporter.export_run") as mock_export_run:
        with patch("app.logging.emit_event"):
            _maybe_export("run-abc", engine)

    mock_export_run.assert_not_called()


@pytest.mark.parametrize("mode", ["note", "structure", "evaluate", "decide"])
@pytest.mark.asyncio
async def test_maybe_export_runs_for_exportable_depths(mode: str):
    """_maybe_export must call export_run for every exportable depth."""
    from app.services.run_finalizer import _maybe_export

    engine = _make_engine(mode=mode)
    with patch("app.services.run_exporter.export_run") as mock_export_run:
        with patch("app.logging.emit_event"):
            _maybe_export("run-abc", engine)

    mock_export_run.assert_called_once()


@pytest.mark.asyncio
async def test_maybe_export_skips_when_run_not_found():
    """_maybe_export must return early when store.get_run returns None."""
    from app.services.run_finalizer import _maybe_export

    engine = _make_engine()
    engine.store.get_run.return_value = None
    with patch("app.services.run_exporter.export_run") as mock_export_run:
        with patch("app.logging.emit_event"):
            _maybe_export("run-abc", engine)

    mock_export_run.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_export_calls_export_run_for_decide_mode(tmp_path):
    """_maybe_export calls export_run and emits the canonical export path."""
    from app.services.run_finalizer import _maybe_export

    engine = _make_engine(mode="decide")
    fake_path = tmp_path / "archive" / "runs" / "example-security-product" / "2026-05-24-run-abc"

    # Both export_run and settings are lazily imported inside _maybe_export;
    # patch at their original locations.
    with patch("app.services.run_exporter.export_run", return_value=fake_path) as mock_export_run:
        with patch("config.settings") as mock_settings:
            with patch("app.logging.emit_event") as mock_emit:
                mock_settings.DECISION_SYSTEM_ROOT = str(tmp_path)
                _maybe_export("run-abc", engine)

    mock_export_run.assert_called_once_with(
        run_id="run-abc",
        store=engine.store,
        decision_system_root=str(tmp_path),
    )
    # Confirm 'exported' event was emitted
    exported_calls = [c for c in mock_emit.call_args_list if c.args[1] == "exported"]
    assert len(exported_calls) == 1
    detail = exported_calls[0].args[3]
    assert detail["path"] == str(fake_path)
    assert detail["canonical_path"] == str(fake_path)
    assert "legacy_path" not in detail


@pytest.mark.asyncio
async def test_maybe_export_emits_skipped_on_os_error():
    """_maybe_export emits export_skipped when export_run raises OSError."""
    from app.services.run_finalizer import _maybe_export

    engine = _make_engine(mode="decide")

    with patch("app.services.run_exporter.export_run", side_effect=OSError("disk full")):
        with patch("config.settings") as mock_settings:
            with patch("app.logging.emit_event") as mock_emit:
                mock_settings.DECISION_SYSTEM_ROOT = "/nonexistent"
                _maybe_export("run-abc", engine)

    skipped_calls = [c for c in mock_emit.call_args_list if c.args[1] == "export_skipped"]
    assert len(skipped_calls) == 1


# ---------------------------------------------------------------------------
# Event emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_action_defaults_to_status():
    """When event_action is None, emit_event should receive the status as action."""
    from app.services.run_finalizer import finalize_run

    engine = _make_engine()
    with patch("app.services.run_finalizer._maybe_export"):
        with patch("app.logging.emit_event") as mock_emit:
            await finalize_run("run-abc", "completed", engine)

    # First call is from finalize_run itself
    first_call = mock_emit.call_args_list[0]
    assert first_call.args[1] == "completed"


@pytest.mark.asyncio
async def test_custom_event_action_is_used():
    """When event_action is provided, it overrides the status in emit_event."""
    from app.services.run_finalizer import finalize_run

    engine = _make_engine()
    with patch("app.services.run_finalizer._maybe_export"):
        with patch("app.logging.emit_event") as mock_emit:
            await finalize_run("run-abc", "killed", engine, event_action="kill_confirmed")

    first_call = mock_emit.call_args_list[0]
    assert first_call.args[1] == "kill_confirmed"


@pytest.mark.asyncio
async def test_event_detail_is_passed_through():
    """event_detail dict is included in the emitted event."""
    from app.services.run_finalizer import finalize_run

    engine = _make_engine()
    detail = {"reason": "Out of scope"}
    with patch("app.services.run_finalizer._maybe_export"):
        with patch("app.logging.emit_event") as mock_emit:
            await finalize_run("run-abc", "killed", engine, event_detail=detail)

    first_call = mock_emit.call_args_list[0]
    assert first_call.args[3] == detail


@pytest.mark.asyncio
async def test_empty_event_detail_uses_empty_dict():
    """When event_detail is None, emit_event receives {}."""
    from app.services.run_finalizer import finalize_run

    engine = _make_engine()
    with patch("app.services.run_finalizer._maybe_export"):
        with patch("app.logging.emit_event") as mock_emit:
            await finalize_run("run-abc", "killed", engine, event_detail=None)

    first_call = mock_emit.call_args_list[0]
    assert first_call.args[3] == {}


# ---------------------------------------------------------------------------
# ended_by derivation (terminal-reason taxonomy)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,event_action,mode,expected",
    [
        # Failure always wins, regardless of prior depth.
        ("failed", None, "decide", "failed"),
        # Semantic actions distinguish reasons that share a status/mode.
        ("completed", "auto_triaged", "archive", "auto_triaged"),  # S2 auto-file
        ("completed", None, "archive", "archived"),                # PM Gate-1 archive
        ("killed", "rejected", "decide", "rejected"),              # Gate 2 reject
        ("killed", "kill_confirmed", "decide", "kill_confirmed"),  # Gate 3 kill
        ("killed", "kill_overridden", "poc", "kill_overridden"),
        ("killed", "voided", "structure", "voided"),               # admin void
        # Plain completion derives the reason from the depth reached.
        ("completed", None, "note", "noted"),
        ("completed", None, "structure", "structured"),
        ("completed", None, "evaluate", "evaluated"),
        ("completed", None, "decide", "decided"),
        # Fallbacks when nothing more specific is available.
        ("killed", None, None, "killed"),
        ("completed", None, None, "completed"),
    ],
)
def test_derive_ended_by(status, event_action, mode, expected):
    from app.services.run_finalizer import _derive_ended_by

    assert _derive_ended_by(status, event_action, mode) == expected


@pytest.mark.asyncio
async def test_finalize_persists_ended_by_on_run():
    """ended_by is stamped on the row, distinguishing a PM archive from an
    auto-triage even though both leave status=completed, mode=archive."""
    from app.services.run_finalizer import finalize_run

    engine = _make_engine(mode="archive", status="running")
    with patch("app.services.run_finalizer._maybe_export"):
        with patch("app.logging.emit_event"):
            await finalize_run(
                "run-abc", "completed", engine, event_action="auto_triaged"
            )

    _, kwargs = engine.store.finish.call_args
    assert kwargs["ended_by"] == "auto_triaged"
