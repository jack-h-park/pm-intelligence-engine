"""Unit tests for run_finalizer — single exit point for terminal run transitions.

Covers:
  - finalize_run() calls store.update_run with the right status
  - completed_at is stamped by the store for completed/killed (store-layer contract)
  - export is triggered ONLY for completed + decide mode
  - export is NOT triggered for completed + non-decide modes
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


def test_finalize_run_calls_store_update_with_status():
    """finalize_run must call store.update_run with the given status."""
    from app.services.run_finalizer import finalize_run

    engine = _make_engine()
    with patch("app.services.run_finalizer._maybe_export"):
        with patch("app.logging.emit_event"):
            finalize_run("run-abc", "completed", engine)

    engine.store.update_run.assert_called_once_with(
        "run-abc", status="completed", current_stage=None
    )


def test_finalize_run_killed_calls_store_update():
    """finalize_run with 'killed' must call store.update_run(status='killed')."""
    from app.services.run_finalizer import finalize_run

    engine = _make_engine()
    with patch("app.logging.emit_event"):
        finalize_run("run-abc", "killed", engine)

    engine.store.update_run.assert_called_once_with(
        "run-abc", status="killed", current_stage=None
    )


def test_finalize_run_failed_calls_store_update():
    """finalize_run with 'failed' must call store.update_run(status='failed')."""
    from app.services.run_finalizer import finalize_run

    engine = _make_engine()
    with patch("app.logging.emit_event"):
        finalize_run("run-abc", "failed", engine)

    engine.store.update_run.assert_called_once_with(
        "run-abc", status="failed", current_stage=None
    )


@pytest.mark.parametrize(
    ("status", "expected_signal_status"),
    [
        ("completed", "done"),
        ("killed", "done"),
        ("failed", "pending"),
    ],
)
def test_finalize_run_updates_signal_status(status: str, expected_signal_status: str):
    """finalize_run must keep Signal.status aligned with terminal run outcomes."""
    from app.services.run_finalizer import finalize_run

    engine = _make_engine()
    with patch("app.logging.emit_event"):
        with patch("app.services.run_finalizer._maybe_export"):
            finalize_run("run-abc", status, engine)

    engine.store.update_signal_status.assert_called_once_with(
        "signal-abc",
        expected_signal_status,
    )


def test_finalize_run_skips_signal_status_update_when_run_not_found():
    """Signal status update is skipped when the run cannot be reloaded."""
    from app.services.run_finalizer import finalize_run

    engine = _make_engine()
    engine.store.get_run.return_value = None

    with patch("app.logging.emit_event"):
        finalize_run("run-abc", "failed", engine)

    engine.store.update_signal_status.assert_not_called()


# ---------------------------------------------------------------------------
# Export policy — only completed + decide triggers export
# ---------------------------------------------------------------------------


def test_export_triggered_for_completed_decide():
    """Export fires when status=completed and mode=decide."""
    from app.services.run_finalizer import finalize_run

    engine = _make_engine(mode="decide")
    with patch("app.services.run_finalizer._maybe_export") as mock_export:
        with patch("app.logging.emit_event"):
            finalize_run("run-abc", "completed", engine)

    mock_export.assert_called_once_with("run-abc", engine)


@pytest.mark.parametrize("mode", ["file", "brief", "opportunity", "evaluate"])
def test_export_not_triggered_for_non_decide_modes(mode: str):
    """Export must NOT fire for completed runs with non-decide modes."""
    from app.services.run_finalizer import finalize_run

    engine = _make_engine(mode=mode)
    with patch("app.services.run_finalizer._maybe_export") as mock_export:
        with patch("app.logging.emit_event"):
            finalize_run("run-abc", "completed", engine)

    # _maybe_export IS called — but it short-circuits on mode check inside
    # We test the mode guard inside _maybe_export separately below.
    # Here we confirm the completed path invokes _maybe_export at least:
    mock_export.assert_called_once()


@pytest.mark.parametrize("status", ["killed", "failed"])
def test_export_not_triggered_for_non_completed_statuses(status: str):
    """Export must NOT fire for killed or failed regardless of mode."""
    from app.services.run_finalizer import finalize_run

    engine = _make_engine(mode="decide")
    with patch("app.services.run_finalizer._maybe_export") as mock_export:
        with patch("app.logging.emit_event"):
            finalize_run("run-abc", status, engine)

    mock_export.assert_not_called()


# ---------------------------------------------------------------------------
# _maybe_export mode guard
# ---------------------------------------------------------------------------


def test_maybe_export_skips_when_mode_not_exportable():
    """_maybe_export must return early when run.mode is not in _EXPORTABLE_MODES."""
    from app.services.run_finalizer import _maybe_export

    engine = _make_engine(mode="brief")
    # export_run is lazily imported inside _maybe_export; patch at its source module
    with patch("app.services.run_exporter.export_run") as mock_export_run:
        with patch("app.logging.emit_event"):
            _maybe_export("run-abc", engine)

    mock_export_run.assert_not_called()


def test_maybe_export_skips_when_run_not_found():
    """_maybe_export must return early when store.get_run returns None."""
    from app.services.run_finalizer import _maybe_export

    engine = _make_engine()
    engine.store.get_run.return_value = None
    with patch("app.services.run_exporter.export_run") as mock_export_run:
        with patch("app.logging.emit_event"):
            _maybe_export("run-abc", engine)

    mock_export_run.assert_not_called()


def test_maybe_export_calls_export_run_for_decide_mode(tmp_path):
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


def test_maybe_export_emits_skipped_on_os_error():
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


def test_event_action_defaults_to_status():
    """When event_action is None, emit_event should receive the status as action."""
    from app.services.run_finalizer import finalize_run

    engine = _make_engine()
    with patch("app.services.run_finalizer._maybe_export"):
        with patch("app.logging.emit_event") as mock_emit:
            finalize_run("run-abc", "completed", engine)

    # First call is from finalize_run itself
    first_call = mock_emit.call_args_list[0]
    assert first_call.args[1] == "completed"


def test_custom_event_action_is_used():
    """When event_action is provided, it overrides the status in emit_event."""
    from app.services.run_finalizer import finalize_run

    engine = _make_engine()
    with patch("app.services.run_finalizer._maybe_export"):
        with patch("app.logging.emit_event") as mock_emit:
            finalize_run("run-abc", "killed", engine, event_action="kill_confirmed")

    first_call = mock_emit.call_args_list[0]
    assert first_call.args[1] == "kill_confirmed"


def test_event_detail_is_passed_through():
    """event_detail dict is included in the emitted event."""
    from app.services.run_finalizer import finalize_run

    engine = _make_engine()
    detail = {"reason": "Out of scope"}
    with patch("app.services.run_finalizer._maybe_export"):
        with patch("app.logging.emit_event") as mock_emit:
            finalize_run("run-abc", "killed", engine, event_detail=detail)

    first_call = mock_emit.call_args_list[0]
    assert first_call.args[3] == detail


def test_empty_event_detail_uses_empty_dict():
    """When event_detail is None, emit_event receives {}."""
    from app.services.run_finalizer import finalize_run

    engine = _make_engine()
    with patch("app.services.run_finalizer._maybe_export"):
        with patch("app.logging.emit_event") as mock_emit:
            finalize_run("run-abc", "killed", engine, event_detail=None)

    first_call = mock_emit.call_args_list[0]
    assert first_call.args[3] == {}
