import hashlib
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_engine
from app.api.main import app
from app.factory import PMEngine
from app.storage.insight_store import InsightStore


@pytest.fixture()
def store_factory(tmp_path):
    database_url = f"sqlite:///{tmp_path}/insights.db"

    def factory() -> InsightStore:
        store = InsightStore(database_url)
        store.initialize_schema()
        return store

    return factory


@pytest.fixture()
def candidate_payload():
    return {
        "origin": "user_supplied",
        "subject": "Managed Android work-profile behavior",
        "question_ids": ["android-enterprise-isolation"],
        "source_ids": [],
        "policy_revision": "fixture-v1",
    }


@pytest.fixture()
def source_payload():
    content = "The user observed selected intent sharing in a managed work profile."
    return {
        "origin": "user_supplied",
        "content_hash": hashlib.sha256(content.encode()).hexdigest(),
        "acquisition_status": "ok",
        "retrieved_at": datetime(2026, 9, 8, tzinfo=UTC).isoformat(),
        "content": content,
        "url": None,
        "legacy_reference": None,
    }


@pytest.fixture()
def bundle_payload():
    def build(candidate_id: str, source_id: str):
        return {
            "candidate_id": candidate_id,
            "source_ids": [source_id],
            "passages": [
                {
                    "passage_id": "passage-fixture-1",
                    "source_id": source_id,
                    "locator": "user statement",
                    "text": "The user observed selected intent sharing in a managed work profile.",
                    "role": "seed",
                }
            ],
            "dates": [
                {"value": None, "kind": "event", "provenance": "not provided"}
            ],
            "coverage_gaps": ["No external verification was supplied."],
            "freshness_status": "unknown",
            "context_revision": "fixture-v1",
        }

    return build


@pytest.fixture()
def auth_headers():
    return {"Authorization": "Bearer insight-test-token"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from config import settings

    insight_store = InsightStore(f"sqlite:///{tmp_path}/api.db")
    insight_store.initialize_schema()
    engine = PMEngine(
        store=None,  # The E01 routes only need the separately injected insight store.
        llm=None,
        context_loader=None,
        template_service=None,
        notifier=None,
        insight_store=insight_store,
    )
    monkeypatch.setattr(settings, "PM_PLATFORM_API_TOKEN", "insight-test-token")
    monkeypatch.setattr(settings, "INSIGHT_WRITES_ENABLED", True)
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite:///{tmp_path}/lifespan.db")
    # The legacy application's lifespan retains its persona-prompt preflight.
    # Build its minimal valid context in tmp_path so the route test remains
    # portable while still exercising the real mounted application's startup.
    decision_context = tmp_path / "decision-context"
    prompts = decision_context / "prompts" / "s4-personas"
    prompts.mkdir(parents=True)
    for persona in ("explorer", "strategist", "builder", "skeptic"):
        (prompts / f"{persona}.md").write_text(
            "## Lens\nFixture lens\n\n## Evaluation Question\nFixture question\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(settings, "DECISION_CONTEXT_ROOT", str(decision_context))
    monkeypatch.setattr(settings, "DECISION_SYSTEM_ROOT", str(decision_context))
    app.dependency_overrides[get_engine] = lambda: engine
    with TestClient(app, raise_server_exceptions=True) as test_client:
        yield test_client
    app.dependency_overrides.clear()
