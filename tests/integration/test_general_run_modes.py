"""Policy tests for product_id='general' mode restrictions.

Verifies that the API enforces file/brief-only for general signals at both
the /runs/start and /runs/{id}/direction endpoints.
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

_BLOCKED_MODES = ["opportunity", "evaluate", "decide"]
_ALLOWED_MODES = ["file", "brief"]


@pytest.fixture()
def engine(tmp_path):
    db_url = f"sqlite:///{tmp_path}/test.db"
    store = SQLiteStore(db_url)
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value="{}")

    context_loader = MagicMock(spec=ContextLoader)
    context_loader.load_full_context.return_value = MagicMock(
        pm_identity="PM identity",
        company_context="Company context",
        product_context="General scope context",
        product_id="general",
    )

    template_service = MagicMock(spec=TemplateService)
    notifier = MagicMock(spec=FanoutNotifier)
    notifier.send = AsyncMock()
    notifier.send_gate1 = AsyncMock()

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
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


def _seed_signal(engine: PMEngine) -> str:
    return engine.store.save_signal(
        product_id="general",
        title="NIST AI RMF update",
        raw_content="Full signal text.",
    )


def _seed_awaiting_direction_run(engine: PMEngine) -> str:
    signal_id = _seed_signal(engine)
    run_id = engine.store.create_run("general", signal_id)
    engine.store.update_run(run_id, status="awaiting_direction", current_stage="s2")
    return run_id


# ---------------------------------------------------------------------------
# /runs/start — upfront mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", _ALLOWED_MODES)
def test_start_run_general_allowed_modes(client, engine, mode):
    signal_id = _seed_signal(engine)
    resp = client.post("/runs/start", json={
        "signal_id": signal_id,
        "product_id": "general",
        "mode": mode,
    })
    assert resp.status_code == 202, resp.text


@pytest.mark.parametrize("mode", _BLOCKED_MODES)
def test_start_run_general_blocked_modes(client, engine, mode):
    signal_id = _seed_signal(engine)
    resp = client.post("/runs/start", json={
        "signal_id": signal_id,
        "product_id": "general",
        "mode": mode,
    })
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "general" in detail
    assert "reassign" in detail.lower() or "specific product" in detail.lower()


# ---------------------------------------------------------------------------
# /runs/{id}/direction — Gate 1 direction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", _ALLOWED_MODES)
def test_direction_general_allowed_modes(client, engine, mode):
    run_id = _seed_awaiting_direction_run(engine)
    resp = client.post(f"/runs/{run_id}/direction", json={"mode": mode})
    assert resp.status_code == 202, resp.text


@pytest.mark.parametrize("mode", _BLOCKED_MODES)
def test_direction_general_blocked_modes(client, engine, mode):
    run_id = _seed_awaiting_direction_run(engine)
    resp = client.post(f"/runs/{run_id}/direction", json={"mode": mode})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "general" in detail
    assert "reassign" in detail.lower() or "specific product" in detail.lower()


# ---------------------------------------------------------------------------
# Error message consistency
# ---------------------------------------------------------------------------


def test_error_message_consistent_across_endpoints(client, engine):
    """Both endpoints should reference 'general' and hint at reassignment."""
    signal_id = _seed_signal(engine)
    run_id = _seed_awaiting_direction_run(engine)

    start_resp = client.post("/runs/start", json={
        "signal_id": signal_id,
        "product_id": "general",
        "mode": "decide",
    })
    direction_resp = client.post(f"/runs/{run_id}/direction", json={"mode": "decide"})

    assert start_resp.status_code == 422
    assert direction_resp.status_code == 422

    for resp in [start_resp, direction_resp]:
        detail = resp.json()["detail"]
        assert "general" in detail
        assert "file" in detail or "brief" in detail


def test_start_run_rejects_mismatched_signal_product(client, engine):
    """Run start must reject caller-supplied product_id that disagrees with the signal."""
    signal_id = engine.store.save_signal(
        product_id="general",
        title="NIST AI RMF update",
        raw_content="Full signal text.",
    )

    resp = client.post("/runs/start", json={
        "signal_id": signal_id,
        "product_id": "example-security-product",
        "mode": "brief",
    })

    assert resp.status_code == 422
    assert "does not match" in resp.json()["detail"]


def test_start_run_marks_signal_in_run(client, engine):
    """A successfully started run must move the source signal into in_run."""
    signal_id = _seed_signal(engine)

    with pytest.MonkeyPatch.context() as mp:
        async def _noop(*args, **kwargs):
            return None

        mp.setattr("app.api.runs._execute_s1_s2", _noop)
        resp = client.post("/runs/start", json={
            "signal_id": signal_id,
            "product_id": "general",
            "mode": "brief",
        })

    assert resp.status_code == 202, resp.text
    signal = engine.store.get_signal(signal_id)
    assert signal is not None
    assert signal["status"] == "in_run"


def test_direction_failure_marks_run_failed_and_signal_pending(client, engine):
    """Direction-path exceptions must use terminal failure semantics."""
    run_id = _seed_awaiting_direction_run(engine)
    signal_id = engine.store.get_run(run_id)["signal_id"]
    engine.store.update_signal_status(signal_id, "in_run")

    with pytest.MonkeyPatch.context() as mp:
        async def _boom(*args, **kwargs):
            raise RuntimeError("direction exploded")

        mp.setattr("app.api.runs._continue_after_direction", _boom)
        resp = client.post(f"/runs/{run_id}/direction", json={"mode": "brief"})

    assert resp.status_code == 202, resp.text

    run = engine.store.get_run(run_id)
    signal = engine.store.get_signal(signal_id)
    assert run is not None
    assert signal is not None
    assert run["status"] == "failed"
    assert run["completed_at"] is None
    assert signal["status"] == "pending"
