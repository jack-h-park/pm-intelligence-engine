import pytest
from unittest.mock import AsyncMock, MagicMock
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


def _seed_run(engine: PMEngine) -> str:
    signal_id = engine.store.save_signal(
        original_product_id="example-security-product",
        title="Android 16 NFC allowlist",
        raw_content="Full signal text.",
    )
    run_id = engine.store.create_run("example-security-product", signal_id)
    engine.store.update_run(run_id, status="completed", mode="decide", current_stage=None)
    return run_id


def test_list_artifacts_returns_saved_artifacts(client, engine):
    run_id = _seed_run(engine)
    engine.store.save_artifact(
        run_id=run_id,
        artifact_type="executive_summary",
        content_md="# Summary",
        content_json='{"markdown":"# Summary"}',
    )

    resp = client.get(f"/runs/{run_id}/artifacts")

    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload) == 1
    assert payload[0]["type"] == "executive_summary"
    assert payload[0]["content_md"] == "# Summary"


def test_list_artifacts_filters_by_type(client, engine):
    run_id = _seed_run(engine)
    engine.store.save_artifact(
        run_id=run_id,
        artifact_type="executive_summary",
        content_md="# Summary",
        content_json='{"markdown":"# Summary"}',
    )
    engine.store.save_artifact(
        run_id=run_id,
        artifact_type="prd",
        content_md="# PRD",
        content_json='{"title":"PRD"}',
    )

    resp = client.get(f"/runs/{run_id}/artifacts", params={"artifact_type": "executive_summary"})

    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload) == 1
    assert payload[0]["type"] == "executive_summary"


def test_list_artifacts_404_when_run_missing(client):
    resp = client.get("/runs/nonexistent/artifacts")
    assert resp.status_code == 404
