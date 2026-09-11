"""Gate 0 skip guard — POST /signals rejects source_ref in the skipped bucket.

When GATE0_STATE_FILE is configured, creating a signal whose source_ref matches
a filename in gate0-state.json's `skipped` bucket returns 409. All other cases
(no GATE0_STATE_FILE, source_ref not skipped, missing/malformed state file,
no source_ref) pass through unchanged.
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_engine, require_auth
from app.api.main import app
from app.api.signals import _check_gate0_skip
from app.factory import PMEngine
from app.services.context_loader import ContextLoader
from app.services.notifier import FanoutNotifier
from app.services.template_service import TemplateService
from app.storage.sqlite_store import SQLiteStore

# ─── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def engine(tmp_path):
    store = SQLiteStore(f"sqlite:///{tmp_path}/test.db")
    llm = AsyncMock()
    context_loader = MagicMock(spec=ContextLoader)
    template_service = MagicMock(spec=TemplateService)
    notifier = MagicMock(spec=FanoutNotifier)
    return PMEngine(
        store=store,
        llm=llm,
        context_loader=context_loader,
        template_service=template_service,
        notifier=notifier,
    )


@pytest.fixture()
def client(engine):
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[require_auth] = lambda: None
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


def _write_state(path, skipped=None, submitted=None, notified=None):
    state = {
        "skipped": skipped or {},
        "submitted": submitted or {},
        "notified": notified or {},
    }
    path.write_text(json.dumps(state))
    return str(path)


# ─── _check_gate0_skip unit tests ─────────────────────────────────────────────

def test_skip_guard_raises_409_when_source_ref_in_skipped(tmp_path, monkeypatch):
    state_path = tmp_path / "gate0-state.json"
    _write_state(
        state_path,
        skipped={"2026-06-01-nist-sp800-207a.md": {"skipped_at": "2026-06-01T00:00:00Z"}},
    )
    monkeypatch.setattr("config.settings.GATE0_STATE_FILE", str(state_path))

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        _check_gate0_skip("2026-06-01-nist-sp800-207a.md")
    assert exc_info.value.status_code == 409
    assert "skipped" in exc_info.value.detail


def test_skip_guard_includes_reason_in_detail(tmp_path, monkeypatch):
    state_path = tmp_path / "gate0-state.json"
    _write_state(
        state_path,
        skipped={
            "file.md": {
                "skipped_at": "2026-06-01T00:00:00Z",
                "reason": "archive target already ingested as signal",
            }
        },
    )
    monkeypatch.setattr("config.settings.GATE0_STATE_FILE", str(state_path))

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        _check_gate0_skip("file.md")
    assert "archive target already ingested" in exc_info.value.detail


def test_skip_guard_passes_when_not_in_skipped(tmp_path, monkeypatch):
    state_path = tmp_path / "gate0-state.json"
    _write_state(state_path, skipped={"other-file.md": {"skipped_at": "2026-06-01T00:00:00Z"}})
    monkeypatch.setattr("config.settings.GATE0_STATE_FILE", str(state_path))

    _check_gate0_skip("my-file.md")  # must not raise


def test_skip_guard_passes_when_in_submitted_not_skipped(tmp_path, monkeypatch):
    state_path = tmp_path / "gate0-state.json"
    _write_state(
        state_path,
        submitted={"file.md": {"submitted_at": "2026-06-01T00:00:00Z", "signal_id": "abc"}},
    )
    monkeypatch.setattr("config.settings.GATE0_STATE_FILE", str(state_path))

    _check_gate0_skip("file.md")  # submitted is not blocked


def test_skip_guard_permissive_when_state_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.GATE0_STATE_FILE", str(tmp_path / "missing.json"))

    _check_gate0_skip("any-file.md")  # must not raise


def test_skip_guard_permissive_when_state_file_malformed(tmp_path, monkeypatch):
    state_path = tmp_path / "gate0-state.json"
    state_path.write_text("not valid json{{")
    monkeypatch.setattr("config.settings.GATE0_STATE_FILE", str(state_path))

    _check_gate0_skip("any-file.md")  # must not raise


def test_skip_guard_disabled_when_no_state_file_configured(monkeypatch):
    monkeypatch.setattr("config.settings.GATE0_STATE_FILE", "")

    _check_gate0_skip("any-file.md")  # must not raise


# ─── Integration tests via POST /signals ──────────────────────────────────────

def test_post_signals_409_when_source_ref_is_skipped(client, tmp_path, monkeypatch):
    fname = "2026-06-01-enterprise-passkey.md"
    state_path = tmp_path / "gate0-state.json"
    _write_state(
        state_path,
        skipped={fname: {"skipped_at": "2026-06-01T00:00:00Z", "reason": "already ingested"}},
    )
    monkeypatch.setattr("config.settings.GATE0_STATE_FILE", str(state_path))

    resp = client.post("/signals", json={
        "title": "Enterprise Passkey 2026",
        "raw_content": "content",
        "source_ref": fname,
        "source_type": "file_watch",
    })
    assert resp.status_code == 409
    assert "skipped" in resp.json()["detail"]


def test_post_signals_201_when_source_ref_not_skipped(client, tmp_path, monkeypatch):
    state_path = tmp_path / "gate0-state.json"
    _write_state(state_path)  # empty skipped bucket
    monkeypatch.setattr("config.settings.GATE0_STATE_FILE", str(state_path))

    resp = client.post("/signals", json={
        "title": "New Signal",
        "raw_content": "content",
        "source_ref": "2026-06-01-new-signal.md",
        "source_type": "file_watch",
    })
    assert resp.status_code == 201


def test_post_signals_201_when_no_source_ref(client, tmp_path, monkeypatch):
    state_path = tmp_path / "gate0-state.json"
    _write_state(state_path, skipped={"file.md": {"skipped_at": "2026-06-01T00:00:00Z"}})
    monkeypatch.setattr("config.settings.GATE0_STATE_FILE", str(state_path))

    resp = client.post("/signals", json={"title": "Manual Signal", "raw_content": "content"})
    assert resp.status_code == 201


def test_post_signals_201_when_gate0_state_file_not_configured(client, monkeypatch):
    monkeypatch.setattr("config.settings.GATE0_STATE_FILE", "")

    resp = client.post("/signals", json={
        "title": "Signal",
        "raw_content": "content",
        "source_ref": "any-file.md",
        "source_type": "file_watch",
    })
    assert resp.status_code == 201
