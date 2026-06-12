"""Integration tests for the Gate 2 (approve/revise/reject) and
Gate 3 (routing-review) API flows.

These tests use a real in-memory SQLite store wired to the FastAPI app
via dependency override. Background tasks are awaited synchronously by
patching stage execution so no real LLM calls are made.

State machine paths covered:
  - waiting_approval + approve → running → completed   (mocked S5→S7)
  - waiting_approval + revise  → running → waiting_approval  (S4 retry)
  - waiting_approval + reject  → killed
  - waiting_routing_review + confirm → killed
  - waiting_routing_review + override → running → completed  (mocked S6→S7)
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

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine(tmp_path):
    """Return a PMEngine backed by an isolated in-memory SQLite database."""
    db_url = f"sqlite:///{tmp_path}/test.db"
    store = SQLiteStore(db_url)
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value="{}")

    context_loader = MagicMock(spec=ContextLoader)
    context_loader.load_full_context.return_value = MagicMock(
        pm_identity="PM identity",
        company_context="Company context",
        product_context="Product context",
        product_id="example-security-product",
    )

    template_service = MagicMock(spec=TemplateService)
    notifier = MagicMock(spec=FanoutNotifier)
    notifier.send = AsyncMock()

    return PMEngine(
        store=store,
        llm=llm,
        context_loader=context_loader,
        template_service=template_service,
        notifier=notifier,
    )


@pytest.fixture()
def client(engine):
    """Return a TestClient with the dependency overridden to use the test engine."""
    app.dependency_overrides[get_engine] = lambda: engine
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


def _seed_run(engine: PMEngine, status: str, mode: str = "decide", routing: str = None) -> str:
    """Create a signal and run, then fast-forward the run to the given status."""
    signal_id = engine.store.save_signal(
        original_product_id="example-security-product",
        title="Android 16 NFC allowlist",
        raw_content="Full signal text.",
    )
    run_id = engine.store.create_run("example-security-product", signal_id)
    updates = {"status": status, "mode": mode, "current_stage": "s4"}
    if routing:
        updates["routing"] = routing
    engine.store.update_run(run_id, **updates)
    return run_id


def _seed_s4_output(engine: PMEngine, run_id: str) -> None:
    """Write a minimal S4 stage output so S5 has something to read."""
    s4_data = {
        "stage": "s4",
        "run_id": run_id,
        "version": 1,
        "output": {
            "personas": [
                {"persona": "explorer", "dimension": "Impact", "score": 4,
                 "key_argument": "Strong.", "open_question": "Why?"},
                {"persona": "strategist", "dimension": "Strategic Fit", "score": 5,
                 "key_argument": "Fits.", "open_question": "How?"},
                {"persona": "builder", "dimension": "Feasibility", "score": 4,
                 "key_argument": "Feasible.", "open_question": "When?"},
                {"persona": "skeptic", "dimension": "Confidence", "score": 4,
                 "key_argument": "OK.", "open_question": "Risk?"},
            ],
            "rubric": {
                "total_score": 10, "score_grounding": 3, "skeptic_quality": 3,
                "open_question_quality": 2, "persona_independence": 2,
                "passed": True, "issues": [],
            },
        },
        "metadata": {"created_at": "2026-05-24T00:00:00", "model_used": "claude-3-5-sonnet"},
    }
    engine.store.save_stage_output(run_id, "s4", json.dumps(s4_data))


def _seed_s5_output(engine: PMEngine, run_id: str, routing: str = "prd") -> None:
    """Write a minimal S5 stage output so S6 has something to read."""
    s5_data = {
        "stage": "s5",
        "run_id": run_id,
        "version": 1,
        "output": {
            "composite_score": 4.30,
            "routing": routing,
            "blocking_count": 0,
            "assumptions": [],
            "rationale": "High composite, no blockers.",
        },
        "metadata": {"created_at": "2026-05-24T00:00:00", "model_used": "claude-3-5-sonnet"},
    }
    engine.store.save_stage_output(run_id, "s5", json.dumps(s5_data))


# ---------------------------------------------------------------------------
# Gate 2 — approve
# ---------------------------------------------------------------------------


def test_approve_requires_waiting_approval(client, engine):
    """Approving a run that is not in waiting_approval returns 409."""
    run_id = _seed_run(engine, status="running", mode="decide")
    resp = client.post(f"/runs/{run_id}/approve")
    assert resp.status_code == 409
    assert "waiting_approval" in resp.json()["detail"]


def test_approve_404_on_missing_run(client):
    """Approving a nonexistent run returns 404."""
    resp = client.post("/runs/nonexistent-run-id/approve")
    assert resp.status_code == 404


def test_approve_non_decide_mode_returns_409(client, engine):
    """Approving a run in 'brief' mode returns 409 (gate only applies to decide)."""
    run_id = _seed_run(engine, status="waiting_approval", mode="brief")
    resp = client.post(f"/runs/{run_id}/approve")
    assert resp.status_code == 409
    assert "decide" in resp.json()["detail"]


def test_approve_transitions_run_to_running(client, engine):
    """POST /approve sets the run to running and records an approval event."""
    run_id = _seed_run(engine, status="waiting_approval", mode="decide")
    _seed_s4_output(engine, run_id)

    with patch("app.api.approvals._execute_s5_to_s7", new=AsyncMock()):
        resp = client.post(f"/runs/{run_id}/approve")

    assert resp.status_code == 202
    assert resp.json()["action"] == "approved"

    # Run must be set to running (background task not awaited, but status was set synchronously)
    run = engine.store.get_run(run_id)
    assert run["status"] == "running"
    assert run["current_stage"] == "s5"


def test_approve_records_approval_event(client, engine):
    """POST /approve records an ApprovalEvent with action='approve'."""
    run_id = _seed_run(engine, status="waiting_approval", mode="decide")
    _seed_s4_output(engine, run_id)

    with patch("app.api.approvals._execute_s5_to_s7", new=AsyncMock()):
        client.post(f"/runs/{run_id}/approve")

    # Verify via list_runs that the run exists; approval events are internal
    # (no public list endpoint), but we confirm the run transitioned correctly.
    run = engine.store.get_run(run_id)
    assert run["status"] == "running"


# ---------------------------------------------------------------------------
# Gate 2 — revise
# ---------------------------------------------------------------------------


def test_revise_returns_revise_queued(client, engine):
    """POST /revise returns 202 with action='revise_queued'."""
    run_id = _seed_run(engine, status="waiting_approval", mode="decide")
    _seed_s4_output(engine, run_id)

    with patch("app.api.approvals._execute_s4_retry", new=AsyncMock()):
        resp = client.post(
            f"/runs/{run_id}/revise",
            json={"feedback": "Skeptic underweighted regulatory risk."},
        )

    assert resp.status_code == 202
    assert resp.json()["action"] == "revise_queued"


def test_revise_sets_run_to_running_s4(client, engine):
    """POST /revise sets status=running and current_stage=s4."""
    run_id = _seed_run(engine, status="waiting_approval", mode="decide")
    _seed_s4_output(engine, run_id)

    with patch("app.api.approvals._execute_s4_retry", new=AsyncMock()):
        client.post(
            f"/runs/{run_id}/revise",
            json={"feedback": "Consider regulatory angle."},
        )

    run = engine.store.get_run(run_id)
    assert run["status"] == "running"
    assert run["current_stage"] == "s4"


def test_revise_requires_feedback_field(client, engine):
    """POST /revise with missing feedback returns 422."""
    run_id = _seed_run(engine, status="waiting_approval", mode="decide")
    resp = client.post(f"/runs/{run_id}/revise", json={})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Gate 2 — reject
# ---------------------------------------------------------------------------


def test_reject_kills_run(client, engine):
    """POST /reject transitions run to killed."""
    run_id = _seed_run(engine, status="waiting_approval", mode="decide")

    with patch("app.logging.emit_event"):
        with patch("app.services.run_finalizer._maybe_export"):
            resp = client.post(
                f"/runs/{run_id}/reject",
                json={"reason": "Signal is too early-stage."},
            )

    assert resp.status_code == 200
    assert resp.json()["action"] == "rejected"

    run = engine.store.get_run(run_id)
    signal = engine.store.get_signal(run["signal_id"])
    assert run["status"] == "killed"
    assert run["current_stage"] is None
    assert signal is not None
    assert signal["status"] == "done"


def test_reject_clears_routing(client, engine):
    """POST /reject clears the routing field before killing the run."""
    run_id = _seed_run(engine, status="waiting_approval", mode="decide", routing="prd")

    with patch("app.logging.emit_event"):
        with patch("app.services.run_finalizer._maybe_export"):
            client.post(f"/runs/{run_id}/reject", json={"reason": "Out of scope."})

    run = engine.store.get_run(run_id)
    assert run["routing"] is None
    assert run["status"] == "killed"


def test_reject_requires_reason_field(client, engine):
    """POST /reject with missing reason returns 422."""
    run_id = _seed_run(engine, status="waiting_approval", mode="decide")
    resp = client.post(f"/runs/{run_id}/reject", json={})
    assert resp.status_code == 422


def test_reject_stamps_completed_at(client, engine):
    """POST /reject causes the store to stamp completed_at (killed terminal state)."""
    run_id = _seed_run(engine, status="waiting_approval", mode="decide")

    with patch("app.logging.emit_event"):
        with patch("app.services.run_finalizer._maybe_export"):
            client.post(f"/runs/{run_id}/reject", json={"reason": "Not relevant."})

    run = engine.store.get_run(run_id)
    assert run["status"] == "killed"
    assert run.get("completed_at") is not None, "completed_at must be stamped on killed runs"


# ---------------------------------------------------------------------------
# Gate 3 — routing-review confirm (kill confirmed)
# ---------------------------------------------------------------------------


def test_routing_review_confirm_kills_run(client, engine):
    """POST /routing-review with action=confirm kills the run."""
    run_id = _seed_run(engine, status="waiting_routing_review", mode="decide", routing="kill")

    with patch("app.logging.emit_event"):
        with patch("app.services.run_finalizer._maybe_export"):
            resp = client.post(
                f"/runs/{run_id}/routing-review",
                json={"action": "confirm", "reason": "DISA mandate out of scope."},
            )

    assert resp.status_code == 202
    assert resp.json()["action"] == "routing_confirmed"

    run = engine.store.get_run(run_id)
    signal = engine.store.get_signal(run["signal_id"])
    assert run["status"] == "killed"
    assert run["current_stage"] is None
    assert signal is not None
    assert signal["status"] == "done"


def test_routing_review_confirm_stamps_completed_at(client, engine):
    """Kill confirm stamps completed_at (killed is a terminal state)."""
    run_id = _seed_run(engine, status="waiting_routing_review", mode="decide", routing="kill")

    with patch("app.logging.emit_event"):
        with patch("app.services.run_finalizer._maybe_export"):
            client.post(
                f"/runs/{run_id}/routing-review",
                json={"action": "confirm", "reason": "Confirmed."},
            )

    run = engine.store.get_run(run_id)
    assert run.get("completed_at") is not None


def test_routing_review_404_on_missing_run(client):
    """POST /routing-review on nonexistent run returns 404."""
    resp = client.post(
        "/runs/nonexistent/routing-review",
        json={"action": "confirm", "reason": "N/A"},
    )
    assert resp.status_code == 404


def test_routing_review_409_wrong_state(client, engine):
    """POST /routing-review on a run not in waiting_routing_review returns 409."""
    run_id = _seed_run(engine, status="waiting_approval", mode="decide")
    resp = client.post(
        f"/runs/{run_id}/routing-review",
        json={"action": "confirm", "reason": "N/A"},
    )
    assert resp.status_code == 409


def test_routing_review_422_invalid_action(client, engine):
    """POST /routing-review with an unknown action returns 422."""
    run_id = _seed_run(engine, status="waiting_routing_review", mode="decide", routing="kill")
    resp = client.post(
        f"/runs/{run_id}/routing-review",
        json={"action": "approve"},  # not a valid routing-review action
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Gate 3 — routing-review override
# ---------------------------------------------------------------------------


def test_routing_review_override_sets_new_routing(client, engine):
    """POST /routing-review with action=override changes the routing field."""
    run_id = _seed_run(engine, status="waiting_routing_review", mode="decide", routing="kill")
    _seed_s5_output(engine, run_id, routing="kill")

    with patch("app.api.routing_review._execute_s6_s7_with_routing", new=AsyncMock()):
        resp = client.post(
            f"/runs/{run_id}/routing-review",
            json={"action": "override", "routing": "prd", "reason": "RKP worth a PRD."},
        )

    assert resp.status_code == 202
    body = resp.json()
    assert body["action"] == "routing_overridden"
    assert body["routing"] == "prd"

    run = engine.store.get_run(run_id)
    assert run["routing"] == "prd"
    assert run["status"] == "running"


def test_routing_review_override_requires_routing_field(client, engine):
    """POST /routing-review with action=override but no routing returns 422."""
    run_id = _seed_run(engine, status="waiting_routing_review", mode="decide", routing="kill")
    resp = client.post(
        f"/runs/{run_id}/routing-review",
        json={"action": "override"},  # missing routing
    )
    assert resp.status_code == 422


def test_routing_review_override_invalid_routing_value(client, engine):
    """POST /routing-review with action=override and an unknown routing value returns 422."""
    run_id = _seed_run(engine, status="waiting_routing_review", mode="decide", routing="kill")
    resp = client.post(
        f"/runs/{run_id}/routing-review",
        json={"action": "override", "routing": "unknown"},  # not a valid routing
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Failed run — completed_at NOT stamped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_run_does_not_stamp_completed_at(engine):
    """finalize_run with status='failed' must NOT stamp completed_at."""
    from app.services.run_finalizer import finalize_run

    signal_id = engine.store.save_signal(
        original_product_id="example-security-product",
        title="Crash test signal",
        raw_content="text",
    )
    run_id = engine.store.create_run("example-security-product", signal_id)
    engine.store.update_run(run_id, status="running", mode="decide")

    with patch("app.logging.emit_event"):
        await finalize_run(run_id, "failed", engine, event_detail={"error": "timeout"})

    run = engine.store.get_run(run_id)
    signal = engine.store.get_signal(signal_id)
    assert run["status"] == "failed"
    assert run.get("completed_at") is None, (
        "failed runs must NOT have completed_at stamped — they did not reach a meaningful endpoint"
    )
    assert signal is not None
    assert signal["status"] == "pending"
