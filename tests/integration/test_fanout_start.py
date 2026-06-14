"""Integration tests for Portfolio Triage fan-out at POST /runs/start (US-49)."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from app.api.deps import get_engine
from app.api.main import app
from app.factory import PMEngine
from app.models.stages import PortfolioTriageOutput, ProductRelevance
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
    context_loader.load_portfolio_profiles.return_value = []
    context_loader.load_pm_identity.return_value = "PM identity"
    notifier = MagicMock(spec=FanoutNotifier)
    return PMEngine(
        store=store,
        llm=llm,
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


def _triage_returning(*relevant, all_products=("prod-a", "prod-b", "prod-c")):
    products = [
        ProductRelevance(
            product_id=pid,
            relevance_score=5 if pid in relevant else 2,
            reason="r",
            relevant=pid in relevant,
        )
        for pid in all_products
    ]
    return PortfolioTriageOutput(signal_id="sig", threshold=4, products=products)


def _seed_signal(engine) -> str:
    return engine.store.save_signal(
        title="Android 16 background API deprecation",
        raw_content="Some MDM background-monitoring APIs are deprecated.",
        source_type="rss",
    )


def test_fanout_starts_one_run_per_relevant_product(client, engine, monkeypatch):
    signal_id = _seed_signal(engine)

    async def fake_triage(**kwargs):
        return _triage_returning("prod-a", "prod-b")

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr("app.stages.portfolio_triage.run", fake_triage)
    monkeypatch.setattr("app.api.runs._execute_s1_s2", _noop)

    resp = client.post("/runs/start", json={"signal_id": signal_id})
    assert resp.status_code == 202, resp.text
    body = resp.json()

    # Envelope shape (not a single RunResponse)
    assert "batch_id" in body and body["batch_id"]
    products = {r["product_id"] for r in body["runs"]}
    assert products == {"prod-a", "prod-b"}
    # all three verdicts are returned for the audit trail
    assert len(body["triage"]) == 3
    # siblings share the batch, and the batch is closed for membership
    assert all(r["batch_id"] == body["batch_id"] for r in body["runs"])
    assert engine.store.get_batch(body["batch_id"])["membership_closed"] is True
    # signal moved into in_run
    assert engine.store.get_signal(signal_id)["status"] == "in_run"


def test_fanout_no_relevant_products_creates_no_runs(client, engine, monkeypatch):
    signal_id = _seed_signal(engine)

    async def fake_triage(**kwargs):
        return _triage_returning()  # nothing relevant

    monkeypatch.setattr("app.stages.portfolio_triage.run", fake_triage)
    monkeypatch.setattr("app.api.runs._execute_s1_s2", AsyncMock())

    resp = client.post("/runs/start", json={"signal_id": signal_id})
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["runs"] == []
    assert len(body["triage"]) == 3  # verdicts still reported
    # no runs spawned -> signal not moved to in_run
    assert engine.store.get_signal(signal_id)["status"] == "new"


def test_fanout_missing_signal_404(client):
    resp = client.post("/runs/start", json={"signal_id": "nope"})
    assert resp.status_code == 404


def test_get_batch_returns_runs_and_synthesis(client, engine, monkeypatch):
    signal_id = _seed_signal(engine)

    async def fake_triage(**kwargs):
        return _triage_returning("prod-a", "prod-b")

    monkeypatch.setattr("app.stages.portfolio_triage.run", fake_triage)
    monkeypatch.setattr("app.api.runs._execute_s1_s2", AsyncMock())

    batch_id = client.post("/runs/start", json={"signal_id": signal_id}).json()["batch_id"]

    resp = client.get(f"/runs/batch/{batch_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["batch_id"] == batch_id
    assert {r["product_id"] for r in body["runs"]} == {"prod-a", "prod-b"}
    assert body["membership_closed"] is True
    # runs never settled (execute is a no-op) -> no synthesis yet
    assert body["synthesis"] is None


def test_get_batch_unknown_404(client):
    assert client.get("/runs/batch/nope").status_code == 404


# --- C-3: manual Portfolio Scan ------------------------------------------------

def _manual_run(engine, product_id="example-mobile-product", status="waiting_direction") -> str:
    """A manually-started single run (no batch), as if it reached Gate 1."""
    signal_id = engine.store.save_signal(
        original_product_id=product_id,
        title="Android 16 background API deprecation",
        raw_content="Some MDM background-monitoring APIs are deprecated.",
    )
    run_id = engine.store.create_run(product_id, signal_id)
    engine.store.update_run(run_id, status=status, current_stage="s2")
    return run_id


def test_scan_fans_out_and_pulls_origin_into_batch(client, engine, monkeypatch):
    run_id = _manual_run(engine)

    async def fake_triage(**kwargs):
        return _triage_returning("prod-b", "prod-c", all_products=("prod-b", "prod-c"))

    monkeypatch.setattr("app.stages.portfolio_triage.run", fake_triage)
    monkeypatch.setattr("app.api.runs._execute_s1_s2", AsyncMock())

    resp = client.post(f"/runs/{run_id}/scan")
    assert resp.status_code == 202, resp.text
    body = resp.json()

    assert body["scanned_run_id"] == run_id
    assert body["batch_id"]
    # new siblings spawned (origin not repeated in runs)
    assert {r["product_id"] for r in body["runs"]} == {"prod-b", "prod-c"}
    # origin run is pulled into the same batch
    assert engine.store.get_run(run_id)["batch_id"] == body["batch_id"]
    # membership closed -> synthesis may fire once all settle
    assert engine.store.get_batch(body["batch_id"])["membership_closed"] is True


def test_scan_no_other_relevant_leaves_run_untouched(client, engine, monkeypatch):
    run_id = _manual_run(engine)

    async def fake_triage(**kwargs):
        return _triage_returning(all_products=("prod-b", "prod-c"))  # none relevant

    monkeypatch.setattr("app.stages.portfolio_triage.run", fake_triage)
    monkeypatch.setattr("app.api.runs._execute_s1_s2", AsyncMock())

    resp = client.post(f"/runs/{run_id}/scan")
    assert resp.status_code == 202
    body = resp.json()
    assert body["batch_id"] is None
    assert body["runs"] == []
    # origin run untouched — no batch created
    assert engine.store.get_run(run_id)["batch_id"] is None


def test_scan_on_already_batched_run_409(client, engine, monkeypatch):
    # a run already in a batch (e.g. product-agnostic fan-out) cannot be re-scanned
    signal_id = engine.store.save_signal(title="S", raw_content="…")
    batch_id = engine.store.create_batch(signal_id)
    run_id = engine.store.create_run("prod-a", signal_id, batch_id=batch_id)

    resp = client.post(f"/runs/{run_id}/scan")
    assert resp.status_code == 409
    assert "already part of" in resp.json()["detail"]


def test_scan_unknown_run_404(client):
    assert client.post("/runs/nope/scan").status_code == 404


def test_scan_works_on_auto_triaged_completed_run(client, engine, monkeypatch):
    # edge case: origin auto-triaged (already completed, never hit Gate 1) —
    # scanning the rest of the portfolio must still work.
    run_id = _manual_run(engine, status="completed")

    async def fake_triage(**kwargs):
        return _triage_returning("prod-b", all_products=("prod-b",))

    monkeypatch.setattr("app.stages.portfolio_triage.run", fake_triage)
    monkeypatch.setattr("app.api.runs._execute_s1_s2", AsyncMock())

    resp = client.post(f"/runs/{run_id}/scan")
    assert resp.status_code == 202
    body = resp.json()
    assert {r["product_id"] for r in body["runs"]} == {"prod-b"}
    assert engine.store.get_run(run_id)["batch_id"] == body["batch_id"]
