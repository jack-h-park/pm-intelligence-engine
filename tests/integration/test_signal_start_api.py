from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.deps import get_engine, require_auth
from app.api.signal_start import router
from app.storage.sqlite_store import SQLiteStore
from config import settings


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PM_PLATFORM_API_TOKEN", "isolated")
    store = SQLiteStore(f"sqlite:///{tmp_path}/api.db")
    app = FastAPI()
    app.include_router(router, dependencies=[Depends(require_auth)])
    app.dependency_overrides[get_engine] = lambda: SimpleNamespace(store=store)
    with TestClient(app) as client:
        yield client, store, {"Authorization": "Bearer isolated"}


def test_actual_app_registration_and_authenticated_complete_snapshot(api, monkeypatch):
    from app.api.main import app

    assert "/signal-start/status" in app.openapi()["paths"]
    client, store, headers = api
    assert client.get("/signal-start/status").status_code == 401
    assert (
        client.get("/signal-start/status", headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )
    sid = store.save_signal(original_product_id=None, title="Manual decision", raw_content="Body")
    with store._engine.begin() as conn:
        conn.execute(
            text("UPDATE signals SET ingested_at='2020-01-01' WHERE signal_id=:id"), {"id": sid}
        )
    response = client.get("/signal-start/status", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["complete"] and data["counts"]["eligible"] == 1
    assert data["items"][0]["signal_id"] == sid and data["items"][0]["title"] == "Manual decision"
    assert data["items"][0]["product_id"] is None and data["snapshot_id"]
    monkeypatch.setattr(settings, "PM_PLATFORM_API_TOKEN", "")
    assert client.get("/signal-start/status", headers=headers).status_code == 503


@pytest.mark.parametrize(
    "query",
    [
        "grace_hours=-1",
        "grace_hours=721",
        "grace_hours=nan",
        "failed_grace_hours=inf",
        "signal_id=invalid",
    ],
)
def test_invalid_query_is_not_a_successful_empty_scan(api, query):
    client, _, headers = api
    assert client.get("/signal-start/status?" + query, headers=headers).status_code == 422


def test_target_not_found_and_store_failure_are_explicit(api, monkeypatch):
    client, store, headers = api
    assert (
        client.get("/signal-start/status?signal_id=" + str(uuid4()), headers=headers).status_code
        == 404
    )
    from sqlalchemy.exc import OperationalError

    def fail(*args):
        raise OperationalError("source unavailable", {}, None)

    monkeypatch.setattr(store, "signal_start_snapshot", fail)
    result = client.get("/signal-start/status", headers=headers)
    assert result.status_code == 503 and "source unavailable" not in result.text


def test_read_does_not_mutate_source_rows_or_monitor_revision(api):
    client, store, headers = api
    sid = store.save_signal(original_product_id="example", title="Decision", raw_content="Body")
    before = store.get_signal(sid)
    revision = store.run_monitoring_status()
    for _ in range(2):
        assert client.get("/signal-start/status", headers=headers).status_code == 200
    assert store.get_signal(sid) == before and store.run_monitoring_status() == revision


def test_response_bound_is_explicit_failure_not_truncated_success(api, monkeypatch):
    from app.services import signal_start_status

    client, store, headers = api
    for i in range(2):
        store.save_signal(original_product_id=None, title=f"Manual {i}", raw_content="Body")
    monkeypatch.setattr(signal_start_status, "MAX_ITEMS", 1)
    response = client.get("/signal-start/status", headers=headers)
    assert response.status_code == 503
    assert "items" not in response.json()
