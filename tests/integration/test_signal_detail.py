"""Integration tests for GET /signals/{id} — the detail endpoint returns the
full raw_content, while the list endpoint (GET /signals) stays lean.

The detail body is what signal-refresh.py's --min-improvement guard reads to
compare the current content length against a recovered capture; the list must
NOT carry every signal's full body (inventory scans would balloon)."""

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


_BODY = "Full article body about an Android banking trojan. " * 20


def test_detail_returns_raw_content(client, engine):
    sid = engine.store.save_signal(
        title="Rokarolla Android Malware", raw_content=_BODY,
        original_product_id="example-mobile-product",
    )
    resp = client.get(f"/signals/{sid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["raw_content"] == _BODY
    assert body["signal_id"] == sid


def test_list_omits_raw_content(client, engine):
    engine.store.save_signal(title="s1", raw_content=_BODY)
    resp = client.get("/signals")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    # Lean list — the full body must not be carried here.
    assert "raw_content" not in items[0]


def test_detail_unknown_signal_404(client):
    assert client.get("/signals/does-not-exist").status_code == 404
