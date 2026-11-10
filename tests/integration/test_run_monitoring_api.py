from types import SimpleNamespace

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_engine, require_auth
from app.api.runs import router
from app.storage.sqlite_store import SQLiteStore
from config import settings


def test_monitor_status_route_is_authenticated_and_list_pagination_works(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PM_PLATFORM_API_TOKEN", "monitor-test-token")
    store = SQLiteStore(f"sqlite:///{tmp_path}/status.db")
    app = FastAPI()
    app.include_router(router, dependencies=[Depends(require_auth)])
    app.dependency_overrides[get_engine] = lambda: SimpleNamespace(store=store)
    with TestClient(app) as client:
        assert client.get("/runs/monitoring/status").status_code == 401
        headers = {"Authorization": "Bearer monitor-test-token"}
        response = client.get("/runs/monitoring/status", headers=headers)
        assert response.status_code == 200
        assert response.json()["active_count"] == 0
        assert client.get("/runs?offset=-1", headers=headers).status_code == 422
        assert client.get("/runs?limit=100&offset=100", headers=headers).json() == []
