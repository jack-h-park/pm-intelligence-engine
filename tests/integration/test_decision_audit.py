"""Integration tests for decision-audit completeness (US-44).

Every human gate decision must be persisted as a labeled calibration datapoint:
- Gate 1 (direction): the mode the PM chose vs the mode S2 suggested
- Gate 3 (routing-review): the routing the PM chose vs what S5 recommended
(Gate 2 approve/revise/reject was already recorded.)
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

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
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value="{}")
    context_loader = MagicMock(spec=ContextLoader)
    template_service = MagicMock(spec=TemplateService)
    notifier = MagicMock(spec=FanoutNotifier)
    return PMEngine(
        store=store, llm=llm, context_loader=context_loader,
        template_service=template_service, notifier=notifier,
    )


@pytest.fixture()
def client(engine):
    app.dependency_overrides[get_engine] = lambda: engine
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


def _actions(engine, run_id):
    return [e["action"] for e in engine.store.get_approval_events(run_id)]


def _feedback(engine, run_id, action):
    return next(
        e["feedback_text"] for e in engine.store.get_approval_events(run_id)
        if e["action"] == action
    )


def test_gate1_direction_decision_recorded(client, engine):
    signal_id = engine.store.save_signal(
        product_id="example-security-product", title="Sig", raw_content="Text."
    )
    run_id = engine.store.create_run("example-security-product", signal_id)
    engine.store.update_run(
        run_id, status="awaiting_direction", current_stage="s2",
        recommendation_json=json.dumps({"suggested_mode": "evaluate", "relevance_score": 5}),
    )

    with patch("app.api.direction._execute_from_direction", new=AsyncMock()):
        resp = client.post(f"/runs/{run_id}/direction", json={"mode": "decide"})
    assert resp.status_code == 202, resp.text

    assert "direction" in _actions(engine, run_id)
    fb = _feedback(engine, run_id, "direction")
    assert "chose=decide" in fb and "suggested=evaluate" in fb  # PM overrode the suggestion


def test_gate3_confirm_decision_recorded(client, engine):
    signal_id = engine.store.save_signal(
        product_id="example-security-product", title="Sig", raw_content="Text."
    )
    run_id = engine.store.create_run("example-security-product", signal_id)
    engine.store.update_run(
        run_id, status="waiting_routing_review", current_stage="s5",
        mode="decide", routing="kill",
    )
    resp = client.post(f"/runs/{run_id}/routing-review", json={"action": "confirm"})
    assert resp.status_code == 202, resp.text

    assert "confirm" in _actions(engine, run_id)
    fb = _feedback(engine, run_id, "confirm")
    assert "chose=kill" in fb and "recommended=kill" in fb


def test_gate3_override_decision_recorded(client, engine):
    signal_id = engine.store.save_signal(
        product_id="example-security-product", title="Sig", raw_content="Text."
    )
    run_id = engine.store.create_run("example-security-product", signal_id)
    engine.store.update_run(
        run_id, status="waiting_routing_review", current_stage="s5",
        mode="decide", routing="kill",
    )
    with patch("app.api.routing_review._execute_s6_s7_with_routing", new=AsyncMock()):
        resp = client.post(
            f"/runs/{run_id}/routing-review",
            json={"action": "override", "routing": "prd", "reason": "transient window worth a fast bet"},
        )
    assert resp.status_code == 202, resp.text

    assert "override" in _actions(engine, run_id)
    fb = _feedback(engine, run_id, "override")
    assert "chose=prd" in fb and "recommended=kill" in fb  # PM overrode S5
    assert "transient window" in fb


def test_decisions_queryable_by_event_filter(client, engine):
    """GET /runs?event=override surfaces runs where the PM overrode routing (US-44)."""
    signal_id = engine.store.save_signal(
        product_id="example-security-product", title="Sig", raw_content="Text."
    )
    run_id = engine.store.create_run("example-security-product", signal_id)
    engine.store.update_run(run_id, status="waiting_routing_review", routing="kill", mode="decide")
    with patch("app.api.routing_review._execute_s6_s7_with_routing", new=AsyncMock()):
        client.post(f"/runs/{run_id}/routing-review", json={"action": "override", "routing": "prd"})

    resp = client.get("/runs", params={"event": "override"})
    assert resp.status_code == 200
    assert run_id in [r["run_id"] for r in resp.json()]
