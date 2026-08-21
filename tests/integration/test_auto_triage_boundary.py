"""Integration tests for the auto-triage relevance boundary (US-28).

Auto-triage logic lives in app/api/runs.py: a run whose S2 relevance_score is
below AUTO_TRIAGE_THRESHOLD (default 3) completes silently as mode=file without
pausing at Gate 1. These tests pin the boundary on both sides:

  relevance_score = threshold - 1 (2)  -> completed, mode=file, no Gate 1
  relevance_score = threshold     (3)  -> waiting_direction, Gate 1 notified

S2 is patched at the stage boundary (app.stages.s2_insight.run) so no LLM is
involved; S1 runs for real (it makes no LLM call).
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_engine
from app.api.main import app
from app.factory import PMEngine
from app.models.stages import S2Output, S2OutputData, StageMetadata
from app.services.context_loader import ContextLoader
from app.services.notifier import FanoutNotifier
from app.services.template_service import TemplateService
from app.storage.sqlite_store import SQLiteStore
from tests.integration.conftest import run_status, seed_run_state


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
        product_context="Product context",
        product_id="example-security-product",
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
        original_product_id="example-security-product",
        title="Marginal consumer-app signal",
        raw_content="Full signal text.",
    )


def _s2_output(
    run_id: str,
    relevance_score: int,
    suggested_mode: str,
    depth_basis: str = "trend_only",
) -> S2Output:
    return S2Output(
        run_id=run_id,
        output=S2OutputData(
            what_changed="A consumer app changed its theme engine.",
            reframing="Consumer UX framing vs no enterprise-security implication.",
            pillar_references=[],
            relevance_explanation="No connection to product strategy pillars.",
            relevance_score=relevance_score,
            depth_basis=depth_basis,
            suggested_mode=suggested_mode,
            suggestion_reasoning="Boundary-test reasoning.",
        ),
        metadata=StageMetadata(model_used="mock"),
    )


def _start_run_with_s2_score(
    client,
    engine,
    relevance_score: int,
    suggested_mode: str,
    *,
    force_gate1: bool = False,
    depth: str | None = None,
    depth_basis: str = "trend_only",
) -> str:
    signal_id = _seed_signal(engine)

    async def fake_s2(input, context, llm, store):  # noqa: A002 - matches stage signature
        return _s2_output(context.run_id, relevance_score, suggested_mode, depth_basis)

    body = {"signal_id": signal_id, "product_id": "example-security-product"}
    if force_gate1:
        body["force_gate1"] = True
    if depth is not None:
        body["depth"] = depth

    with patch("app.stages.s2_insight.run", side_effect=fake_s2), patch(
        "config.settings.AUTO_TRIAGE_LOCAL_ARCHIVE_ENABLED", False
    ):
        resp = client.post("/runs/start", json=body)
    assert resp.status_code == 202, resp.text
    return resp.json()["run_id"]


def test_relevance_below_threshold_auto_triages(client, engine):
    run_id = _start_run_with_s2_score(client, engine, relevance_score=2, suggested_mode="file")

    run = engine.store.get_run(run_id)
    assert run_status(run) == "completed"
    assert run["mode"] == "archive"  # US-43: file → archive
    assert run["completed_at"] is not None
    # Gate 1 was never reached
    engine.notifier.send_gate1.assert_not_awaited()
    # Recommendation is still recorded for later inspection
    rec = json.loads(run["recommendation_json"])
    assert rec["relevance_score"] == 2


def test_relevance_at_threshold_pauses_at_gate1(client, engine):
    run_id = _start_run_with_s2_score(client, engine, relevance_score=3, suggested_mode="brief")

    run = engine.store.get_run(run_id)
    assert run_status(run) == "waiting_direction"
    assert run["mode"] is None
    assert run["completed_at"] is None
    engine.notifier.send_gate1.assert_awaited_once()


def test_auto_triage_syncs_signal_status_done(client, engine):
    run_id = _start_run_with_s2_score(client, engine, relevance_score=1, suggested_mode="file")

    run = engine.store.get_run(run_id)
    signal = engine.store.get_signal(run["signal_id"])
    assert signal["status"] == "done"


# ---------------------------------------------------------------------------
# force_gate1 — PM-initiated interactive starts suppress S2 auto-triage
# ---------------------------------------------------------------------------


def test_force_gate1_below_threshold_pauses_at_gate1(client, engine):
    """A below-threshold relevance that would auto-triage instead pauses at Gate 1
    when force_gate1 is set, so the PM always makes the direction decision."""
    run_id = _start_run_with_s2_score(
        client, engine, relevance_score=2, suggested_mode="file", force_gate1=True
    )

    run = engine.store.get_run(run_id)
    assert run_status(run) == "waiting_direction"
    assert run["mode"] is None
    assert run["completed_at"] is None
    engine.notifier.send_gate1.assert_awaited_once()
    # Not recorded as auto-triaged → not reopen-eligible (it was never archived)
    actions = [e["action"] for e in engine.store.get_approval_events(run_id)]
    assert "auto_triaged" not in actions


def test_force_gate1_does_not_affect_above_threshold(client, engine):
    """force_gate1 is a no-op when relevance already clears the threshold — the run
    pauses at Gate 1 exactly as it would without the flag."""
    run_id = _start_run_with_s2_score(
        client, engine, relevance_score=3, suggested_mode="brief", force_gate1=True
    )
    run = engine.store.get_run(run_id)
    assert run_status(run) == "waiting_direction"
    engine.notifier.send_gate1.assert_awaited_once()


def test_force_gate1_with_explicit_depth_still_skips_gate1(client, engine):
    """An explicit depth always wins: force_gate1 has no effect when the PM stated a
    depth, so the run processes immediately rather than pausing at Gate 1."""
    run_id = _start_run_with_s2_score(
        client, engine, relevance_score=2, suggested_mode="file",
        force_gate1=True, depth="evaluate",
    )
    run = engine.store.get_run(run_id)
    assert run_status(run) != "waiting_direction"
    engine.notifier.send_gate1.assert_not_awaited()


def test_no_force_gate1_below_threshold_still_auto_triages(client, engine):
    """Regression guard: without force_gate1, below-threshold still auto-archives
    (the default autonomous path is unchanged)."""
    run_id = _start_run_with_s2_score(client, engine, relevance_score=2, suggested_mode="file")
    run = engine.store.get_run(run_id)
    assert run_status(run) == "completed"
    assert run["mode"] == "archive"
    engine.notifier.send_gate1.assert_not_awaited()


# ---------------------------------------------------------------------------
# US-31 — auto-triage safety net: queryability + reopen
# ---------------------------------------------------------------------------


def test_auto_triaged_runs_queryable_by_event(client, engine):
    triaged_id = _start_run_with_s2_score(client, engine, relevance_score=2, suggested_mode="file")
    gated_id = _start_run_with_s2_score(client, engine, relevance_score=4, suggested_mode="evaluate")

    resp = client.get("/runs", params={"event": "auto_triaged"})
    assert resp.status_code == 200
    ids = [r["run_id"] for r in resp.json()]
    assert triaged_id in ids
    assert gated_id not in ids


def test_list_runs_rejects_unknown_event(client):
    resp = client.get("/runs", params={"event": "bogus"})
    assert resp.status_code == 422


def test_list_runs_since_filter(client, engine):
    run_id = _start_run_with_s2_score(client, engine, relevance_score=2, suggested_mode="file")

    resp = client.get("/runs", params={"event": "auto_triaged", "since": "2000-01-01T00:00:00"})
    assert run_id in [r["run_id"] for r in resp.json()]

    resp = client.get("/runs", params={"event": "auto_triaged", "since": "2999-01-01T00:00:00"})
    assert run_id not in [r["run_id"] for r in resp.json()]

    resp = client.get("/runs", params={"since": "not-a-date"})
    assert resp.status_code == 422


def test_reopen_revives_auto_triaged_run(client, engine):
    run_id = _start_run_with_s2_score(client, engine, relevance_score=2, suggested_mode="file")

    resp = client.post(f"/runs/{run_id}/reopen")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Single-vocabulary surface (US-55): the run state is lifecycle + position,
    # not `status`. Reopen returns the run to Gate 1 = paused at s2.
    assert body["lifecycle"] == "paused"
    assert body["position"] == "s2"
    assert "status" not in body  # legacy field no longer surfaced
    assert body.get("depth") is None  # depth cleared on reopen
    assert body["completed_at"] is None

    run = engine.store.get_run(run_id)
    signal = engine.store.get_signal(run["signal_id"])
    assert signal["status"] == "in_run"
    actions = [e["action"] for e in engine.store.get_approval_events(run_id)]
    assert actions == ["auto_triaged", "reopen"]


def test_reopen_rejected_for_non_auto_triaged_run(client, engine):
    """A deliberate PM decision is not undone by reopen."""
    run_id = _start_run_with_s2_score(client, engine, relevance_score=4, suggested_mode="evaluate")
    # Run paused at Gate 1 — never auto-triaged
    resp = client.post(f"/runs/{run_id}/reopen")
    assert resp.status_code == 409


def test_reopen_twice_rejected(client, engine):
    run_id = _start_run_with_s2_score(client, engine, relevance_score=2, suggested_mode="file")
    assert client.post(f"/runs/{run_id}/reopen").status_code == 200
    assert client.post(f"/runs/{run_id}/reopen").status_code == 409


# ── the stored recommendation carries the GROUNDS, not just the conclusion ───
#
# #67 split the depth decision off relevance so the basis could be stated and checked.
# The basis was then dropped when the recommendation was persisted, so every stored
# recommendation held a conclusion with no grounds — unauditable, and indistinguishable
# from one produced by the suggester #67 replaced. The control-plane decision ledger
# averaged 46 pre-#67 decisions with 8 post-#67 ones into a single agreement rate
# because nothing in the row said which suggester made it.


@pytest.mark.parametrize("basis", ["named_gap", "commit_ready", "no_product_surface"])
def test_the_recorded_recommendation_carries_the_depth_basis(client, engine, basis):
    """Parametrised so a hardcoded default cannot pass: each run must persist the basis
    S2 actually emitted."""
    run_id = _start_run_with_s2_score(
        client, engine, relevance_score=4, suggested_mode="brief", depth_basis=basis,
    )

    rec = json.loads(engine.store.get_run(run_id)["recommendation_json"])
    assert rec["depth_basis"] == basis


def test_the_basis_is_recorded_even_when_the_run_auto_triages(client, engine):
    """The runs that never reach Gate 1 are exactly the ones nobody sees, so their
    grounds are the ones most worth having on record."""
    run_id = _start_run_with_s2_score(
        client, engine, relevance_score=2, suggested_mode="file",
        depth_basis="no_product_surface",
    )

    run = engine.store.get_run(run_id)
    assert run_status(run) == "completed"
    rec = json.loads(run["recommendation_json"])
    assert rec["depth_basis"] == "no_product_surface"
    # Stored normalised, not raw: the model maps the legacy ladder names on input
    # (US-43 file→archive). The control-plane ledger carries the same alias map, and
    # comparing the two raw would count a rename as a disagreement.
    assert rec["suggested_mode"] == "archive"
