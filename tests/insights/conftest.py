import hashlib
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_engine
from app.api.main import app
from app.factory import PMEngine
from app.models.insights import (
    Candidate,
    EvidenceBundle,
    InsightClaim,
    InsightRevision,
    Passage,
    PreparedContext,
    SourceRecord,
)
from app.services.context_loader import ContextLoader
from app.storage.insight_store import InsightStore
from app.storage.sqlite_store import SQLiteStore


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
            "provenance_status": "attributable",
            "novelty_status": "new",
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
    monkeypatch.setattr(settings, "INTELLIGENCE_MODE", "shadow")
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


@pytest.fixture()
def engine_with_insight():
    """Factory: an engine over a two-product decision root plus one saved Insight."""

    def build(tmp_path, *, content: str) -> tuple[PMEngine, InsightRevision]:
        decision_root = tmp_path / "decision-context"
        android = decision_root / "products" / "android-enterprise"
        android.mkdir(parents=True)
        (android / "context.md").write_text(
            "# Android Enterprise\n\n## Product Overview\nManaged Android.\n"
            "\n## Connection Anchors\n- managed work profile\n- cross-profile interaction\n",
            encoding="utf-8",
        )
        mobile = decision_root / "products" / "example-mobile-product"
        mobile.mkdir(parents=True)
        (mobile / "context.md").write_text(
            "# Example Mobile Product\n\n## Product Overview\nMobile threat defense.\n"
            "\n## Connection Anchors\n- malware detection\n",
            encoding="utf-8",
        )
        prompts = decision_root / "prompts" / "s4-personas"
        prompts.mkdir(parents=True)
        for persona in ("explorer", "strategist", "builder", "skeptic"):
            (prompts / f"{persona}.md").write_text(
                "## Lens\nFixture lens\n\n## Evaluation Question\nFixture question\n",
                encoding="utf-8",
            )

        insight_store = InsightStore(f"sqlite:///{tmp_path}/insights.db")
        insight_store.initialize_schema()
        candidate = insight_store.save_candidate(
            Candidate(
                candidate_id="candidate-connection",
                origin="user_supplied",
                subject="Managed profile behavior",
                question_ids=["android"],
                policy_revision="fixture-v1",
            ).model_dump()
        )
        source = insight_store.save_source(
            SourceRecord(
                source_id="source-connection",
                candidate_id=candidate.candidate_id,
                origin="user_supplied",
                content_hash=hashlib.sha256(content.encode()).hexdigest(),
                acquisition_status="ok",
                retrieved_at=datetime(2026, 9, 13, tzinfo=UTC),
                content=content,
            ).model_dump(mode="json")
        )
        bundle = insight_store.save_bundle(
            EvidenceBundle(
                bundle_id="bundle-connection",
                candidate_id=candidate.candidate_id,
                source_ids=[source.source_id],
                passages=[
                    Passage(
                        passage_id="passage-connection",
                        source_id=source.source_id,
                        locator="fixture",
                        text=content,
                        role="seed",
                    )
                ],
                freshness_status="current",
                context_revision="fixture-v1",
            ).model_dump(mode="json")
        )
        prepared = insight_store.save_prepared_context(
            PreparedContext(
                prepared_context_id="prepared-connection",
                candidate_id=candidate.candidate_id,
                bundle_id=bundle.bundle_id,
                question="What changed?",
                validation_status="valid",
                context_revision="fixture-v1",
            ).model_dump(mode="json")
        )
        insight = insight_store.save_insight(
            InsightRevision(
                insight_id="insight-connection",
                prepared_context_id=prepared.prepared_context_id,
                headline="Managed profile finding",
                explanation="A bounded fixture finding.",
                actual_change="A managed work profile changed.",
                why_now="The fixture is current.",
                personal_relevance="It affects managed Android.",
                takeaway="Review the boundary.",
                claims=[
                    InsightClaim(text="The behavior is cited.", passage_ids=["passage-connection"])
                ],
                context_revision="fixture-v1",
            ).model_dump(mode="json")
        )
        return (
            PMEngine(
                store=SQLiteStore(f"sqlite:///{tmp_path}/workflow.db"),
                llm=None,  # type: ignore[arg-type]
                context_loader=ContextLoader(str(decision_root)),
                template_service=None,  # type: ignore[arg-type]
                notifier=None,  # type: ignore[arg-type]
                insight_store=insight_store,
            ),
            insight,
        )

    return build
