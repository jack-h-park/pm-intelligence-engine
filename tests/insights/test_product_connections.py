import hashlib
from datetime import UTC, datetime

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
from app.services.product_connections import ProductConnectionService
from app.storage.insight_store import InsightStore
from app.storage.sqlite_store import SQLiteStore


def _engine_with_insight(tmp_path, *, content: str) -> tuple[PMEngine, InsightRevision]:
    decision_root = tmp_path / "decision-context"
    android = decision_root / "products" / "android-enterprise"
    android.mkdir(parents=True)
    (android / "context.md").write_text(
        "# Android Enterprise\n\n## Product Overview\nManaged Android.\n"
        "\n## Connection Anchors\n- managed work profile\n- cross-profile interaction\n",
        encoding="utf-8",
    )
    knox = decision_root / "products" / "samsung-knox-mtd"
    knox.mkdir(parents=True)
    (knox / "context.md").write_text(
        "# Samsung Knox MTD\n\n## Product Overview\nMobile threat defense.\n"
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
            llm=None,
            context_loader=ContextLoader(str(decision_root)),
            template_service=None,
            notifier=None,
            insight_store=insight_store,
        ),
        insight,
    )


def test_connection_service_returns_only_evidence_anchored_candidate(tmp_path):
    engine, insight = _engine_with_insight(
        tmp_path, content="A managed work profile permits a selected cross-profile interaction."
    )

    assessment = ProductConnectionService(engine.context_loader).assess(
        insight, engine.insight_store
    )

    assert assessment.assessment == "candidates"
    assert [candidate.product_id for candidate in assessment.candidates] == ["android-enterprise"]
    assert assessment.candidates[0].passage_ids == ["passage-connection"]
    assert assessment.candidates[0].confidence == "medium"
    assert engine.insight_store.operational_summary()["feedback"]["recorded"] == 0
    assert engine.store.list_runs(limit=10) == []


def test_connection_service_returns_no_connection_without_a_cited_anchor(tmp_path):
    engine, insight = _engine_with_insight(
        tmp_path, content="A general market report described a distant platform trend."
    )

    assessment = ProductConnectionService(engine.context_loader).assess(
        insight, engine.insight_store
    )

    assert assessment.assessment == "no_clear_connection"
    assert assessment.candidates == []
    assert "No reviewed product connection anchor" in assessment.reason


def test_product_connections_endpoint_is_read_only_and_rejects_stale_revision(
    tmp_path, monkeypatch
):
    from config import settings

    engine, insight = _engine_with_insight(
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
