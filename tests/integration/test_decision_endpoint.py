"""Integration tests for POST /runs/{id}/decision — the unified gate endpoint.

Verifies the (action × run-state) dispatch reaches the same outcome as the legacy
per-gate endpoints it fronts. The old endpoints stay live during the transition;
these tests pin the facade's routing so the eventual cutover (removing the old
routes) is safe.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_engine
from app.api.main import app
from app.factory import PMEngine
from app.services.context_loader import ContextLoader, FullContext
from app.services.notifier import FanoutNotifier
from app.services.template_service import TemplateService
from app.storage.sqlite_store import SQLiteStore
from tests.integration.conftest import run_status, seed_run_state


@pytest.fixture()
def engine(tmp_path):
    store = SQLiteStore(f"sqlite:///{tmp_path}/test.db")
    context_loader = MagicMock(spec=ContextLoader)
    context_loader.load_full_context.return_value = FullContext(
        pm_identity="pm", company_context="co", product_context="prod",
        product_id="example-security-product",
    )
    context_loader.load_product_context.return_value = "ctx"
    notifier = MagicMock(spec=FanoutNotifier)
    notifier.send_gate1 = AsyncMock()
    notifier.send_gate2 = AsyncMock()
    notifier.send_gate3 = AsyncMock()
    return PMEngine(
        store=store, llm=AsyncMock(),
        context_loader=context_loader,
        template_service=MagicMock(spec=TemplateService),
        notifier=notifier,
    )


@pytest.fixture()
def client(engine):
    app.dependency_overrides[get_engine] = lambda: engine
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


_S2 = {
    "stage": "s2", "version": 1,
    "output": {
        "what_changed": "x", "reframing": "a vs b", "pillar_references": ["P1"],
        "claims": [{"text": "f", "source": "signal", "grounds": []},
                   {"text": "c", "source": "inference", "grounds": [1]}],
        "relevance_score": 4, "suggested_mode": "structure", "suggestion_reasoning": "r",
    },
}


def _seed(engine: PMEngine, status: str, mode=None, routing=None, with_s2=True, with_s4=False):
    signal_id = engine.store.save_signal(
        original_product_id="example-security-product", title="Sig", raw_content="Body.",
    )
    run_id = engine.store.create_run("example-security-product", signal_id)
    if with_s2:
        engine.store.save_stage_output(run_id, "s2", json.dumps(dict(_S2, run_id=run_id)))
    if with_s4:
        s4 = {"stage": "s4", "version": 1, "output": {
            "personas": [{"persona": p, "dimension": d, "score": 4, "key_argument": "a", "open_question": "q"}
                         for p, d in [("explorer", "Impact"), ("strategist", "Strategic Fit"),
                                      ("builder", "Feasibility"), ("skeptic", "Confidence")]],
            "rubric": {"total_score": 10, "score_grounding": 3, "skeptic_quality": 3,
                       "open_question_quality": 2, "persona_independence": 2, "passed": True, "issues": []},
        }}
        engine.store.save_stage_output(run_id, "s4", json.dumps(dict(s4, run_id=run_id)))
    seed_run_state(engine.store, run_id, status, mode=mode)
    if routing:
        engine.store.update_run(run_id, routing=routing)
    engine.store.update_signal_status(signal_id, "in_run")
    return run_id, signal_id


# --- Gate 1 ---------------------------------------------------------------


def test_advance_to_at_gate1_sets_depth(client, engine, monkeypatch):
    run_id, _ = _seed(engine, "waiting_direction")
    # Don't actually run the pipeline; just prove direction was applied.
    monkeypatch.setattr("app.api.direction._execute_from_direction", AsyncMock())
    resp = client.post(f"/runs/{run_id}/decision", json={"action": "advance_to", "target": "structure"})
    assert resp.status_code == 202, resp.text
    assert resp.json()["depth"] == "structure"
    assert engine.store.get_run(run_id)["mode"] == "structure"


def test_advance_without_target_at_gate1_is_409(client, engine):
    run_id, _ = _seed(engine, "waiting_direction")
    resp = client.post(f"/runs/{run_id}/decision", json={"action": "advance"})
    assert resp.status_code == 409
    assert "advance_to" in resp.json()["detail"]


# --- Gate 2 ---------------------------------------------------------------


def test_advance_at_gate2_approves(client, engine, monkeypatch):
    run_id, _ = _seed(engine, "waiting_approval", mode="decide", with_s4=True)
    monkeypatch.setattr("app.api.approvals._execute_s5_to_s7", AsyncMock())
    resp = client.post(f"/runs/{run_id}/decision", json={"action": "advance"})
    assert resp.status_code == 202, resp.text
    assert resp.json()["action"] == "approved"
    events = [e["action"] for e in engine.store.get_approval_events(run_id)]
    assert "approve" in events


def test_revise_at_gate2(client, engine, monkeypatch):
    run_id, _ = _seed(engine, "waiting_approval", mode="decide", with_s4=True)
    monkeypatch.setattr("app.api.approvals._execute_s4_retry", AsyncMock())
    resp = client.post(f"/runs/{run_id}/decision", json={"action": "revise", "feedback": "tighten scope"})
    assert resp.status_code == 202, resp.text
    events = engine.store.get_approval_events(run_id)
    assert events[-1]["action"] == "revise"
    assert events[-1]["feedback_text"] == "tighten scope"


def test_stop_at_gate2_rejects(client, engine):
    run_id, _ = _seed(engine, "waiting_approval", mode="decide", with_s4=True)
    resp = client.post(f"/runs/{run_id}/decision", json={"action": "stop", "reason": "not now"})
    assert resp.status_code == 202, resp.text
    run = engine.store.get_run(run_id)
    assert run_status(run) == "killed"
    assert run["ended_by"] == "rejected"


# --- Gate 3 ---------------------------------------------------------------


def test_advance_at_gate3_confirms(client, engine, monkeypatch):
    run_id, _ = _seed(engine, "waiting_routing_review", mode="decide", routing="poc")
    monkeypatch.setattr("app.api.routing_review._execute_s6_s7_with_routing", AsyncMock())
    resp = client.post(f"/runs/{run_id}/decision", json={"action": "advance"})
    assert resp.status_code == 202, resp.text
    assert resp.json()["routing"] == "poc"


def test_advance_to_at_gate3_overrides_routing(client, engine, monkeypatch):
    run_id, _ = _seed(engine, "waiting_routing_review", mode="decide", routing="kill")
    monkeypatch.setattr("app.api.routing_review._execute_s6_s7_with_routing", AsyncMock())
    resp = client.post(f"/runs/{run_id}/decision", json={"action": "advance_to", "routing": "poc"})
    assert resp.status_code == 202, resp.text
    assert resp.json()["routing"] == "poc"
    assert engine.store.get_run(run_id)["routing"] == "poc"


def test_stop_at_gate3_kills(client, engine):
    run_id, _ = _seed(engine, "waiting_routing_review", mode="decide", routing="prd")
    resp = client.post(f"/runs/{run_id}/decision", json={"action": "stop", "reason": "reconsidered"})
    assert resp.status_code == 202, resp.text
    assert run_status(engine.store.get_run(run_id)) == "killed"


# --- Completed: deepen / reopen ------------------------------------------


def test_advance_to_on_completed_deepens(client, engine, monkeypatch):
    run_id, _ = _seed(engine, "completed", mode="structure")
    monkeypatch.setattr("app.api.deepen._execute_deepen", AsyncMock())
    resp = client.post(f"/runs/{run_id}/decision", json={"action": "advance_to", "target": "evaluate"})
    assert resp.status_code == 202, resp.text
    assert resp.json()["action"] == "deepen_started"
    assert resp.json()["depth"] == "evaluate"


def test_advance_on_completed_reopens_auto_triaged(client, engine):
    run_id, signal_id = _seed(engine, "completed", mode="archive")
    engine.store.record_approval(run_id=run_id, stage="s2", action="auto_triaged")
    resp = client.post(f"/runs/{run_id}/decision", json={"action": "advance"})
    assert resp.status_code == 202, resp.text
    assert run_status(engine.store.get_run(run_id)) == "waiting_direction"


# --- Void (any non-terminal) ---------------------------------------------


def test_stop_on_running_voids(client, engine):
    run_id, _ = _seed(engine, "running")
    resp = client.post(f"/runs/{run_id}/decision", json={"action": "stop", "reason": "started in error"})
    assert resp.status_code == 202, resp.text
    run = engine.store.get_run(run_id)
    assert run_status(run) == "killed"
    assert run["ended_by"] == "voided"


# --- Validation -----------------------------------------------------------


def test_unknown_action_422(client, engine):
    run_id, _ = _seed(engine, "waiting_approval", mode="decide", with_s4=True)
    resp = client.post(f"/runs/{run_id}/decision", json={"action": "frobnicate"})
    assert resp.status_code == 422


def test_unknown_run_404(client):
    resp = client.post("/runs/nope/decision", json={"action": "advance"})
    assert resp.status_code == 404


def test_stop_on_terminal_run_409(client, engine):
    run_id, _ = _seed(engine, "completed", mode="decide")
    resp = client.post(f"/runs/{run_id}/decision", json={"action": "stop", "reason": "x"})
    assert resp.status_code == 409
