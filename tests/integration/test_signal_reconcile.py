"""Integration test for POST /signals/reconcile against a real SQLite store.

Reproduces the 2026-06-21 "Glasswing" divergence: a signal holding a terminal
``completed`` run that was created outside ``finalize_run`` (a synthetic /
backfilled row) and so never had its signal status synced — leaving the signal
stuck at ``in_run`` instead of ``done``. The reconcile endpoint must repair it.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_engine
from app.api.main import app
from app.factory import PMEngine
from app.services.context_loader import ContextLoader
from app.services.notifier import FanoutNotifier
from app.services.template_service import TemplateService
from app.storage.sqlite_store import SQLiteStore
from tests.integration.conftest import run_status, seed_run_state


@pytest.fixture()
def engine(tmp_path):
    store = SQLiteStore(f"sqlite:///{tmp_path}/test.db")
    llm = AsyncMock()
    return PMEngine(
        store=store,
        llm=llm,
        context_loader=MagicMock(spec=ContextLoader),
        template_service=MagicMock(spec=TemplateService),
        notifier=MagicMock(spec=FanoutNotifier),
    )


@pytest.fixture()
def client(engine):
    app.dependency_overrides[get_engine] = lambda: engine
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


def test_reconcile_fixes_signal_stuck_at_in_run(client, engine):
    # A signal with a completed run, but the signal was left at in_run because the
    # run was created directly (bypassing finalize_run) — the Glasswing case.
    signal_id = engine.store.save_signal(
        original_product_id="example-security-product",
        title="Anthropic Glasswing Initial Update",
        raw_content="Signal body.",
    )
    run_id = engine.store.create_run("enterprise-ai-agents", signal_id)
    seed_run_state(engine.store, run_id, "completed")
    engine.store.update_signal_status(signal_id, "in_run")  # the stale state

    assert engine.store.get_signal(signal_id)["status"] == "in_run"

    resp = client.post("/signals/reconcile")
    assert resp.status_code == 200
    body = resp.json()

    assert body["corrected"] == 1
    assert body["changes"] == [
        {
            "signal_id": signal_id,
            "title": "Anthropic Glasswing Initial Update",
            "old": "in_run",
            "new": "done",
        }
    ]
    assert engine.store.get_signal(signal_id)["status"] == "done"


def test_reconcile_is_idempotent(client, engine):
    signal_id = engine.store.save_signal(
        original_product_id="example-security-product",
        title="Already correct",
        raw_content="Signal body.",
    )
    run_id = engine.store.create_run("example-security-product", signal_id)
    seed_run_state(engine.store, run_id, "completed")
    engine.store.update_signal_status(signal_id, "done")

    first = client.post("/signals/reconcile").json()
    assert first["corrected"] == 0

    # A second call still finds nothing to fix.
    second = client.post("/signals/reconcile").json()
    assert second["corrected"] == 0
    assert engine.store.get_signal(signal_id)["status"] == "done"


def test_reconcile_keeps_signal_in_run_while_a_sibling_is_unsettled(client, engine):
    # Fan-out: one run completed, one still running → signal stays in_run.
    signal_id = engine.store.save_signal(
        original_product_id="example-security-product",
        title="Fan-out in flight",
        raw_content="Signal body.",
    )
    done_run = engine.store.create_run("product-a", signal_id)
    seed_run_state(engine.store, done_run, "completed")
    live_run = engine.store.create_run("product-b", signal_id)
    seed_run_state(engine.store, live_run, "running")
    engine.store.update_signal_status(signal_id, "in_run")

    resp = client.post("/signals/reconcile").json()
    assert resp["corrected"] == 0
    assert engine.store.get_signal(signal_id)["status"] == "in_run"
