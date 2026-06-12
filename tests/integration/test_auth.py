"""Server-side API authentication tests (P0 — Authenticate pm-engine).

Verifies that every endpoint except GET /health requires a valid
`Authorization: Bearer <PM_PLATFORM_API_TOKEN>` header.
"""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_engine, require_auth
from app.api.main import app

_TOKEN = "test-secret-token-abc123"


@pytest.fixture()
def engine():
    """Minimal engine whose store returns a signal on create.

    Only the surface exercised by POST /signals is mocked; auth is enforced
    before the handler body runs, so unauthenticated cases never touch it.
    """
    eng = MagicMock()
    eng.store.save_signal.return_value = "sig-1"
    eng.store.get_signal.return_value = {
        "signal_id": "sig-1",
        "original_product_id": "example-security-product",
        "title": "Test signal",
        "source_url": None,
        "category": "other",
        "status": "pending",
        "source_type": "manual",
        "ingested_at": "2026-06-05T00:00:00",
    }
    return eng


@pytest.fixture()
def client(engine, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "PM_PLATFORM_API_TOKEN", _TOKEN)
    # The conftest autouse fixture bypasses auth for business-logic tests;
    # remove it here so real enforcement runs.
    app.dependency_overrides.pop(require_auth, None)
    app.dependency_overrides[get_engine] = lambda: engine
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


_SIGNAL_BODY = {
    "product_id": "example-security-product",
    "title": "Test signal",
    "raw_content": "body",
}


def test_health_requires_no_token(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_write_endpoint_rejects_missing_token(client):
    resp = client.post("/signals", json=_SIGNAL_BODY)
    assert resp.status_code == 401


def test_write_endpoint_rejects_wrong_token(client):
    resp = client.post(
        "/signals",
        json=_SIGNAL_BODY,
        headers={"Authorization": "Bearer not-the-real-token"},
    )
    assert resp.status_code == 401


def test_write_endpoint_rejects_malformed_header(client):
    resp = client.post(
        "/signals",
        json=_SIGNAL_BODY,
        headers={"Authorization": _TOKEN},  # missing "Bearer " scheme
    )
    assert resp.status_code == 401


def test_write_endpoint_accepts_valid_token(client):
    resp = client.post(
        "/signals",
        json=_SIGNAL_BODY,
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert resp.status_code == 201
    assert resp.json()["signal_id"] == "sig-1"


def test_read_endpoint_also_requires_token(client):
    assert client.get("/signals").status_code == 401
    ok = client.get("/signals", headers={"Authorization": f"Bearer {_TOKEN}"})
    assert ok.status_code == 200


def test_fails_closed_when_server_token_unset(engine, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "PM_PLATFORM_API_TOKEN", "")
    app.dependency_overrides.pop(require_auth, None)
    app.dependency_overrides[get_engine] = lambda: engine
    try:
        with TestClient(app, raise_server_exceptions=True) as c:
            # No token can authenticate; fail closed with 503, not open access.
            resp = c.post(
                "/signals",
                json=_SIGNAL_BODY,
                headers={"Authorization": "Bearer anything"},
            )
            assert resp.status_code == 503
            # Health stays up so liveness probes / launchd KeepAlive don't thrash.
            assert c.get("/health").status_code == 200
    finally:
        app.dependency_overrides.clear()
