import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_engine
from app.api.main import app

CONNECTED = "A managed work profile permits a selected cross-profile interaction."


@pytest.fixture
def client_for(tmp_path, monkeypatch, engine_with_insight):
    from config import settings

    def make(*, mode, trial="", v2=True, content=CONNECTED):
        engine, insight = engine_with_insight(tmp_path, content=content)
        root = str(tmp_path / "decision-context")
        monkeypatch.setattr(settings, "DECISION_CONTEXT_ROOT", root)
        monkeypatch.setattr(settings, "DECISION_SYSTEM_ROOT", root)
        monkeypatch.setattr(settings, "PM_PLATFORM_API_TOKEN", "suggestion-token")
        monkeypatch.setattr(settings, "DECISION_PIPELINE_V2_ENABLED", v2)
        monkeypatch.setattr(settings, "INSIGHT_DECISION_SUGGESTIONS", mode)
        monkeypatch.setattr(settings, "INSIGHT_DECISION_SUGGESTION_TRIAL_IDS", trial)
        app.dependency_overrides[get_engine] = lambda: engine
        return TestClient(app), insight, engine

    yield make
    app.dependency_overrides.clear()


H = {"Authorization": "Bearer suggestion-token"}


@pytest.mark.parametrize(
    "mode,trial,v2,state",
    [
        ("on", "", True, "shown"),
        ("off", "", True, "withheld"),
        ("on", "", False, "withheld"),
        ("trial", "insight-connection", True, "shown"),
        ("trial", "someone-else", True, "withheld"),
    ],
)
def test_single_route_states(client_for, mode, trial, v2, state):
    client, insight, engine = client_for(mode=mode, trial=trial, v2=v2)
    body = client.get(f"/insights/{insight.insight_id}/decision-suggestion", headers=H).json()
    assert body["state"] == state
    assert bool(body["products"]) is (state == "shown")
    assert "product_id" not in body
    assert engine.store.list_runs(limit=10) == []


def test_single_route_requires_auth_and_a_known_insight(client_for):
    client, insight, _ = client_for(mode="on")
    assert client.get(f"/insights/{insight.insight_id}/decision-suggestion").status_code in (
        401,
        403,
    )
    assert client.get("/insights/unknown/decision-suggestion", headers=H).status_code == 404


def test_list_route_requires_auth(client_for):
    client, _, _ = client_for(mode="on")
    assert client.get("/insight-decision-suggestions").status_code in (401, 403)


def test_a_superseded_insight_answers_none(client_for):
    client, insight, engine = client_for(mode="on")
    engine.insight_store.save_insight(
        insight.model_copy(
            update={
                "insight_id": "insight-connection-r2",
                "supersedes_insight_id": insight.insight_id,
                "revision": 2,
            }
        ).model_dump(mode="json")
    )
    old = client.get(f"/insights/{insight.insight_id}/decision-suggestion", headers=H).json()
    new = client.get("/insights/insight-connection-r2/decision-suggestion", headers=H).json()
    assert old["state"] == "none"
    assert new["state"] == "shown"


@pytest.mark.parametrize("mode", ["off", "trial", "on"])
def test_list_route_reports_would_suggest_in_every_state(client_for, mode):
    client, insight, _ = client_for(mode=mode)
    items = client.get("/insight-decision-suggestions", headers=H).json()["items"]
    assert [(i["insight_id"], i["would_suggest"]) for i in items] == [(insight.insight_id, True)]


def test_list_route_lists_products_even_when_withheld(client_for):
    client, insight, _ = client_for(mode="off")
    (item,) = client.get("/insight-decision-suggestions", headers=H).json()["items"]
    assert item["state"] == "withheld"
    assert item["products"] and item["products"][0]["product_id"]
    assert "product_id" not in item


def _save_needs_evidence_insight(engine, insight):
    prepared = engine.insight_store.get_prepared_context(insight.prepared_context_id)
    engine.insight_store.save_prepared_context(
        prepared.model_copy(
            update={
                "prepared_context_id": "prepared-needs-evidence",
                "validation_status": "needs_evidence",
            }
        ).model_dump(mode="json")
    )
    return engine.insight_store.save_insight(
        insight.model_copy(
            update={
                "insight_id": "insight-needs-evidence",
                "prepared_context_id": "prepared-needs-evidence",
            }
        ).model_dump(mode="json")
    )


def test_an_insight_the_reading_surfaces_hide_answers_none(client_for):
    client, insight, engine = client_for(mode="on")
    hidden = _save_needs_evidence_insight(engine, insight)
    body = client.get(f"/insights/{hidden.insight_id}/decision-suggestion", headers=H).json()
    assert body["state"] == "none"
    assert body["products"] == []


def test_list_route_does_not_suggest_a_hidden_insight(client_for):
    client, insight, engine = client_for(mode="on")
    hidden = _save_needs_evidence_insight(engine, insight)
    items = client.get("/insight-decision-suggestions", headers=H).json()["items"]
    by_id = {i["insight_id"]: i for i in items}
    assert by_id[hidden.insight_id]["would_suggest"] is False
    assert "reading surfaces" in by_id[hidden.insight_id]["reason"]
    assert by_id[insight.insight_id]["would_suggest"] is True
