"""Policy tests for product_id='general' mode restrictions.

Verifies that the API enforces file/brief-only for general signals at both
the /runs/start and /runs/{id}/direction endpoints.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient

from app.api.main import app
from app.api.deps import get_engine
from app.factory import PMEngine
from app.storage.sqlite_store import SQLiteStore
from app.services.context_loader import ContextLoader
from app.services.template_service import TemplateService
from app.services.notifier import FanoutNotifier


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
