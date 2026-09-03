"""Integration tests for POST /runs/{id}/deepen — resume a completed run deeper.

Deepen converts the Gate 1 depth choice from a one-shot forecast into an
incremental pull: a run completed at note/structure can be resumed at a deeper
depth, reusing stored S2/S3/S4 outputs and executing only the stages the new
depth adds. Production data motivated this: 17 Gate 1 decisions, zero `decide`
choices — the upfront commitment was too expensive at the moment of least
information.
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
        pm_identity="pm",
        company_context="company",
        product_context="product",
        product_id="example-security-product",
    )
    return PMEngine(
        store=store,
        llm=AsyncMock(),
        context_loader=context_loader,
        template_service=MagicMock(spec=TemplateService),
        notifier=MagicMock(spec=FanoutNotifier),
    )


@pytest.fixture()
def client(engine):
    app.dependency_overrides[get_engine] = lambda: engine
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


_S2_OUTPUT = {
    "stage": "s2",
    "version": 1,
    "output": {
        "what_changed": "Something changed.",
        "reframing": "market framing vs product framing",
        "pillar_references": ["Pillar 1"],
        "claims": [
            {"text": "Fact from signal.", "source": "signal", "grounds": []},
            {"text": "Derived conclusion.", "source": "inference", "grounds": [1]},
        ],
        "relevance_score": 4,
        "suggested_mode": "structure",
        "suggestion_reasoning": "Concrete opportunity.",
    },
}

_S3_OUTPUT = {
    "stage": "s3",
    "version": 1,
    "output": {
        "problem_statement": "Users lack X.",
        "target_user": "IT admins at regulated enterprises",
        "hypothesis": "If we do X, then Y will happen, because Z.",
        "assumed_value_user": "X saves admin time.",
        "assumed_value_business": "X differentiates the product.",
        "value_horizon": "durable",
    },
}


def _seed_completed_run(engine: PMEngine, mode: str, stages: list[str]) -> tuple[str, str]:
    """A completed run at `mode` with stored outputs for `stages`."""
    signal_id = engine.store.save_signal(
        original_product_id="example-security-product",
        title="Deepen candidate signal",
        raw_content="Body.",
    )
    run_id = engine.store.create_run("example-security-product", signal_id)
    if "s2" in stages:
        s2 = dict(_S2_OUTPUT, run_id=run_id)
        engine.store.save_stage_output(run_id, "s2", json.dumps(s2))
    if "s3" in stages:
        s3 = dict(_S3_OUTPUT, run_id=run_id)
        engine.store.save_stage_output(run_id, "s3", json.dumps(s3))
    seed_run_state(engine.store, run_id, "completed", mode=mode)
    engine.store.update_signal_status(signal_id, "done")
    return run_id, signal_id


def test_deepen_note_to_structure_runs_only_s3(client, engine, monkeypatch):
    """The headline case: note → structure resumes at S3, skipping S1/S2."""
    run_id, signal_id = _seed_completed_run(engine, "note", stages=["s2"])

    ran = []

    async def fake_s3_run(input, context, llm, store):  # noqa: A002
        ran.append("s3")
        s3 = dict(_S3_OUTPUT, run_id=run_id)
        store.save_stage_output(run_id, "s3", json.dumps(s3))
        from app.models.stages import S3Output, S3OutputData, StageMetadata
        return S3Output(
            run_id=run_id,
            output=S3OutputData(**_S3_OUTPUT["output"]),
            metadata=StageMetadata(model_used="test"),
        )

    from app.stages import s3_opportunity
    monkeypatch.setattr(s3_opportunity, "run", fake_s3_run)

    resp = client.post(f"/runs/{run_id}/deepen", json={"depth": "structure"})
    assert resp.status_code == 202
    body = resp.json()
    assert body["action"] == "deepen_started"
    assert body["from_depth"] == "note"
    assert body["depth"] == "structure"

    run = engine.store.get_run(run_id)
    assert run_status(run) == "completed"           # background task ran to finalize
    assert run["mode"] == "structure"
    assert run["completed_at"] is not None
    assert ran == ["s3"]

    # Labeled calibration datapoint, symmetric with Gate 1 `direction`.
    events = engine.store.get_approval_events(run_id)
    assert [e["action"] for e in events] == ["deepen"]
    assert events[0]["feedback_text"] == "from=note; to=structure"

    assert engine.store.get_signal(signal_id)["status"] == "done"


def test_deepen_structure_to_decide_reuses_s3_and_pauses_at_gate2(client, engine, monkeypatch):
    """structure → decide skips the stored S3, runs S4, pauses at Gate 2."""
    run_id, _ = _seed_completed_run(engine, "structure", stages=["s2", "s3"])

    ran = []

    async def fail_s3_run(*args, **kwargs):
        ran.append("s3")
        raise AssertionError("S3 must be reused from the store, not re-run")

    async def fake_s4_run(input, context, llm, store):  # noqa: A002
        ran.append("s4")
        from app.models.stages import (
            PersonaOutput,
            S4Output,
            S4OutputData,
            S4RubricResult,
            StageMetadata,
        )
        personas = [
            PersonaOutput(
                persona=p, dimension=d, score=4,
                key_argument="arg", open_question="q?",
            )
            for p, d in [
                ("explorer", "Impact"), ("strategist", "Strategic Fit"),
                ("builder", "Feasibility"), ("skeptic", "Confidence"),
            ]
        ]
        rubric = S4RubricResult(
            total_score=10, score_grounding=3, skeptic_quality=3,
            open_question_quality=2, persona_independence=2, passed=True,
        )
        out = S4Output(
            run_id=run_id,
            output=S4OutputData(personas=personas, rubric=rubric),
            metadata=StageMetadata(model_used="test"),
        )
        store.save_stage_output(run_id, "s4", out.model_dump_json())
        return out

    from app.stages import s3_opportunity, s4_evaluation
    monkeypatch.setattr(s3_opportunity, "run", fail_s3_run)
    monkeypatch.setattr(s4_evaluation, "run", fake_s4_run)
    engine.notifier.send_gate2 = AsyncMock()

    resp = client.post(f"/runs/{run_id}/deepen", json={"depth": "decide"})
    assert resp.status_code == 202

    run = engine.store.get_run(run_id)
    assert run_status(run) == "waiting_approval"    # Gate 2, as any decide run
    assert run["mode"] == "decide"
    assert run["completed_at"] is None            # no longer terminal
    assert ran == ["s4"]
    engine.notifier.send_gate2.assert_awaited_once()


def test_deepen_rejects_shallower_or_equal_depth(client, engine):
    run_id, _ = _seed_completed_run(engine, "structure", stages=["s2", "s3"])
    for depth in ("archive", "note", "structure"):
        resp = client.post(f"/runs/{run_id}/deepen", json={"depth": depth})
        assert resp.status_code == 409
        assert "not deeper" in resp.json()["detail"]


@pytest.mark.parametrize("status", ["running", "waiting_direction", "waiting_approval", "killed", "failed"])
def test_deepen_rejects_non_completed_runs(client, engine, status):
    run_id, _ = _seed_completed_run(engine, "note", stages=["s2"])
    seed_run_state(engine.store, run_id, status)
    resp = client.post(f"/runs/{run_id}/deepen", json={"depth": "structure"})
    assert resp.status_code == 409


def test_deepen_rejects_run_without_depth(client, engine):
    """A completed run with no mode (e.g. legacy row) points to /reopen instead."""
    run_id, _ = _seed_completed_run(engine, "note", stages=["s2"])
    engine.store.update_run(run_id, mode=None)
    resp = client.post(f"/runs/{run_id}/deepen", json={"depth": "structure"})
    assert resp.status_code == 409
    assert "reopen" in resp.json()["detail"]


def test_deepen_accepts_legacy_mode_alias(client, engine, monkeypatch):
    """`mode` body key and legacy value `opportunity` normalize to structure."""
    run_id, _ = _seed_completed_run(engine, "note", stages=["s2"])

    async def fake_s3_run(input, context, llm, store):  # noqa: A002
        from app.models.stages import S3Output, S3OutputData, StageMetadata
        return S3Output(
            run_id=run_id,
            output=S3OutputData(**_S3_OUTPUT["output"]),
            metadata=StageMetadata(model_used="test"),
        )

    from app.stages import s3_opportunity
    monkeypatch.setattr(s3_opportunity, "run", fake_s3_run)

    resp = client.post(f"/runs/{run_id}/deepen", json={"mode": "opportunity"})
    assert resp.status_code == 202
    assert resp.json()["depth"] == "structure"


def test_deepen_unknown_run_404(client):
    resp = client.post("/runs/nope/deepen", json={"depth": "decide"})
    assert resp.status_code == 404


def test_deepen_invalid_depth_422(client, engine):
    run_id, _ = _seed_completed_run(engine, "note", stages=["s2"])
    resp = client.post(f"/runs/{run_id}/deepen", json={"depth": "bogus"})
    assert resp.status_code == 422


def test_deepen_filterable_in_run_list(client, engine, monkeypatch):
    """GET /runs?event=deepen surfaces deepened runs for calibration review."""
    run_id, _ = _seed_completed_run(engine, "note", stages=["s2"])

    async def fake_s3_run(input, context, llm, store):  # noqa: A002
        from app.models.stages import S3Output, S3OutputData, StageMetadata
        return S3Output(
            run_id=run_id,
            output=S3OutputData(**_S3_OUTPUT["output"]),
            metadata=StageMetadata(model_used="test"),
        )

    from app.stages import s3_opportunity
    monkeypatch.setattr(s3_opportunity, "run", fake_s3_run)

    client.post(f"/runs/{run_id}/deepen", json={"depth": "structure"})
    resp = client.get("/runs", params={"event": "deepen"})
    assert resp.status_code == 200
    assert [r["run_id"] for r in resp.json()] == [run_id]


def test_run_response_includes_review_url(client, engine):
    """Every run object carries review_url so the delivery owner (the ops plane) can put
    the Gate 2 review link in messages without knowing the engine's BASE_URL
    (NOTIFICATION_CONTRACT §2, US-50)."""
    run_id, _ = _seed_completed_run(engine, "note", stages=["s2"])
    resp = client.get(f"/runs/{run_id}")
    assert resp.status_code == 200
    from config import settings
    assert resp.json()["review_url"] == f"{settings.BASE_URL}/runs/{run_id}/review"
