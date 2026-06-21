"""Integration tests for POST /runs/{id}/void — administratively cancel a run.

Covers the gap from the 2026-06-21 b906e4d7 incident: a run at
`waiting_direction` had no kill path (`reject` is decide-mode/`waiting_approval`
only; `archive` records the run as completed evaluated work). `void` ends the run
as `killed` with a reason, distinct from `archive`, usable from any non-terminal
state.
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


@pytest.fixture()
def engine(tmp_path):
    store = SQLiteStore(f"sqlite:///{tmp_path}/test.db")
    return PMEngine(
        store=store,
        llm=AsyncMock(),
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


def _seed_run(engine: PMEngine, status: str, mode: str | None = None) -> tuple[str, str]:
    signal_id = engine.store.save_signal(
        original_product_id="example-security-product",
        title="Improperly started signal",
        raw_content="Body.",
    )
    run_id = engine.store.create_run("example-governance-product", signal_id)
    updates = {"status": status}
    if mode:
        updates["mode"] = mode
    engine.store.update_run(run_id, **updates)
    engine.store.update_signal_status(signal_id, "in_run")
    return run_id, signal_id


def test_void_from_waiting_direction(client, engine):
    """The headline case: kill a run stuck at Gate 1 with no other exit."""
    run_id, signal_id = _seed_run(engine, "waiting_direction")

    resp = client.post(f"/runs/{run_id}/void", json={"reason": "wrong product, Gate 0 misread"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "voided"
    assert body["voided_from"] == "waiting_direction"

    run = engine.store.get_run(run_id)
    assert run["status"] == "killed"
    assert run["completed_at"] is not None          # killed stamps completed_at
    assert run["mode"] is None                       # NOT archive — never evaluated

    # Distinct from a routing kill / Gate 2 reject: recorded as a `void` event.
    events = engine.store.get_approval_events(run_id)
    assert [e["action"] for e in events] == ["void"]
    assert events[0]["feedback_text"] == "wrong product, Gate 0 misread"

    # Signal settles to done once its only run is terminal.
    assert engine.store.get_signal(signal_id)["status"] == "done"


@pytest.mark.parametrize("status", ["pending", "running", "waiting_approval", "waiting_routing_review"])
def test_void_from_other_non_terminal_states(client, engine, status):
    run_id, _ = _seed_run(engine, status)
    resp = client.post(f"/runs/{run_id}/void", json={"reason": "void it"})
    assert resp.status_code == 200
    assert engine.store.get_run(run_id)["status"] == "killed"


@pytest.mark.parametrize("status", ["completed", "killed", "failed"])
def test_void_rejected_for_terminal_runs(client, engine, status):
    run_id, _ = _seed_run(engine, status)
    resp = client.post(f"/runs/{run_id}/void", json={"reason": "too late"})
    assert resp.status_code == 409
    assert "terminal" in resp.json()["detail"].lower()


def test_void_clears_routing(client, engine):
    """A run that reached S5 routing is voided as killed, not as a routing kill."""
    run_id, _ = _seed_run(engine, "waiting_routing_review")
    engine.store.update_run(run_id, routing="prd")

    resp = client.post(f"/runs/{run_id}/void", json={"reason": "started in error"})
    assert resp.status_code == 200

    run = engine.store.get_run(run_id)
    assert run["status"] == "killed"
    assert run["routing"] is None


def test_void_unknown_run_404(client):
    resp = client.post("/runs/does-not-exist/void", json={"reason": "x"})
    assert resp.status_code == 404


def test_void_requires_reason(client, engine):
    run_id, _ = _seed_run(engine, "waiting_direction")
    resp = client.post(f"/runs/{run_id}/void", json={})
    assert resp.status_code == 422
