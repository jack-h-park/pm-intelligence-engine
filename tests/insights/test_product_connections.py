from fastapi.testclient import TestClient

from app.api.deps import get_engine
from app.api.main import app
from app.services.product_connections import ProductConnectionService


def test_connection_service_returns_only_evidence_anchored_candidate(tmp_path, engine_with_insight):
    engine, insight = engine_with_insight(
        tmp_path, content="A managed work profile permits a selected cross-profile interaction."
    )

    assessment = ProductConnectionService(engine.context_loader).assess(
        insight, engine.insight_store
    )

    assert assessment.assessment == "candidates"
    assert [candidate.product_id for candidate in assessment.candidates] == [
        "android-enterprise"
    ]
    assert assessment.candidates[0].passage_ids == ["passage-connection"]
    assert assessment.candidates[0].confidence == "medium"
    assert engine.insight_store.operational_summary()["feedback"]["recorded"] == 0
    assert engine.store.list_runs(limit=10) == []


def test_connection_service_returns_no_connection_without_a_cited_anchor(
    tmp_path, engine_with_insight
):
    engine, insight = engine_with_insight(
        tmp_path, content="A general market report described a distant platform trend."
    )

    assessment = ProductConnectionService(engine.context_loader).assess(
        insight, engine.insight_store
    )

    assert assessment.assessment == "no_clear_connection"
    assert assessment.candidates == []
    assert "No reviewed product connection anchor" in assessment.reason


def test_product_connections_endpoint_is_read_only_and_rejects_stale_revision(
    tmp_path, monkeypatch, engine_with_insight
):
    from config import settings

    engine, insight = engine_with_insight(
        tmp_path, content="A managed work profile permits a selected cross-profile interaction."
    )
    monkeypatch.setattr(settings, "DECISION_CONTEXT_ROOT", str(tmp_path / "decision-context"))
    monkeypatch.setattr(settings, "DECISION_SYSTEM_ROOT", str(tmp_path / "decision-context"))
    monkeypatch.setattr(settings, "PM_PLATFORM_API_TOKEN", "connection-token")
    app.dependency_overrides[get_engine] = lambda: engine
    try:
        with TestClient(app, raise_server_exceptions=True) as client:
            headers = {"Authorization": "Bearer connection-token"}
            ok = client.get(
                f"/insights/{insight.insight_id}/product-connections?revision=1", headers=headers
            )
            stale = client.get(
                f"/insights/{insight.insight_id}/product-connections?revision=2", headers=headers
            )
    finally:
        app.dependency_overrides.clear()

    assert ok.status_code == 200
    assert ok.json()["assessment"] == "candidates"
    assert stale.status_code == 409
    assert engine.store.list_runs(limit=10) == []
    assert engine.insight_store.operational_summary()["feedback"]["recorded"] == 0
