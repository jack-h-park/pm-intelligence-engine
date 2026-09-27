"""Authenticated effective-state evidence for Insight cutover and rollback."""

from config import settings


def test_runtime_status_exposes_effective_mode_and_release_switches(
    client, auth_headers, monkeypatch,
):
    monkeypatch.setattr(settings, "INTELLIGENCE_MODE", "insights")
    monkeypatch.setattr(settings, "INSIGHT_WRITES_ENABLED", True)
    monkeypatch.setattr(settings, "DECISION_PIPELINE_V2_ENABLED", True)
    monkeypatch.setattr(settings, "INSIGHT_MIGRATION_ACTIVATION_ENABLED", False)
    monkeypatch.setattr(settings, "INSIGHT_LEGACY_IMPORT_ENABLED", False)
    monkeypatch.setattr(settings, "INSIGHT_PROJECTION_ENABLED", True)

    response = client.get("/intelligence/runtime", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {
        "mode": "insights",
        "insight_writes_enabled": True,
        "decision_v2_enabled": True,
        "migration_activation_enabled": False,
        "legacy_import_enabled": False,
        "projection_enabled": True,
    }


def test_runtime_status_is_authenticated_and_normalizes_unknown_mode(
    client, auth_headers, monkeypatch,
):
    monkeypatch.setattr(settings, "INTELLIGENCE_MODE", "not-a-mode")
    assert client.get("/intelligence/runtime").status_code == 401
    response = client.get("/intelligence/runtime", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["mode"] == "legacy"
