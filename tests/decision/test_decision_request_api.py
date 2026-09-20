import hashlib
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.api.deps import get_engine
from app.api.main import app
from app.factory import PMEngine
from app.models.decision_case import InsightRevisionReference
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


def test_decision_request_is_idempotent_and_schedules_only_once(tmp_path, monkeypatch):
    from app.api import runs
    from config import settings

    async def no_op(*args, **kwargs):
        return None

    monkeypatch.setattr(runs, "_execute_s1_s2", no_op)
    decision_root = tmp_path / "decision-context"
    (decision_root / "products" / "android-enterprise").mkdir(parents=True)
    (decision_root / "products" / "android-enterprise" / "context.md").write_text(
        "# Android Enterprise\n\n## Product Overview\nFixture.", encoding="utf-8"
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
            candidate_id="candidate-api",
            origin="user_supplied",
            subject="Managed policy behavior",
            question_ids=["android"],
            policy_revision="fixture-v1",
        ).model_dump()
    )
    content = "A managed profile exposed a selected sharing target."
    source = insight_store.save_source(
        SourceRecord(
            source_id="source-api",
            candidate_id=candidate.candidate_id,
            origin="user_supplied",
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            acquisition_status="ok",
            retrieved_at=datetime(2026, 9, 8, tzinfo=UTC),
            content=content,
        ).model_dump(mode="json")
    )
    bundle = insight_store.save_bundle(
        EvidenceBundle(
            bundle_id="bundle-api",
            candidate_id=candidate.candidate_id,
            source_ids=[source.source_id],
            passages=[
                Passage(
                    passage_id="passage-api",
                    source_id=source.source_id,
                    locator="fixture",
                    text=content,
                    role="seed",
                )
            ],
            freshness_status="unknown",
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )
    prepared = insight_store.save_prepared_context(
        PreparedContext(
            prepared_context_id="prepared-api",
            revision=2,
            candidate_id=candidate.candidate_id,
            bundle_id=bundle.bundle_id,
            question="What should we decide?",
            validation_status="valid",
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )
    insufficient = insight_store.save_prepared_context(
        PreparedContext(
            prepared_context_id="prepared-insufficient-api",
            revision=1,
            candidate_id=candidate.candidate_id,
            bundle_id=bundle.bundle_id,
            question="What still needs evidence?",
            validation_status="needs_evidence",
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )
    insight = insight_store.save_insight(
        InsightRevision(
            insight_id="insight-decision-api",
            revision=3,
            prepared_context_id=prepared.prepared_context_id,
            headline="Managed profile finding",
            explanation="A managed profile changed.",
            actual_change="The profile can now expose a sharing target.",
            why_now="The behavior was newly observed.",
            personal_relevance="It affects enterprise policy.",
            takeaway="Assess the policy boundary.",
            claims=[InsightClaim(text="The target was exposed.", passage_ids=["passage-api"])],
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )
    engine = PMEngine(
        store=SQLiteStore(f"sqlite:///{tmp_path}/workflow.db"),
        llm=None,
        context_loader=ContextLoader(str(decision_root)),
        template_service=None,
        notifier=None,
        insight_store=insight_store,
    )
    monkeypatch.setattr(settings, "PM_PLATFORM_API_TOKEN", "decision-token")
    monkeypatch.setattr(settings, "INSIGHT_WRITES_ENABLED", True)
    monkeypatch.setattr(settings, "DECISION_CONTEXT_ROOT", str(decision_root))
    monkeypatch.setattr(settings, "DECISION_SYSTEM_ROOT", str(decision_root))
    app.dependency_overrides[get_engine] = lambda: engine
    headers = {"Authorization": "Bearer decision-token", "Idempotency-Key": "request-api"}
    payload = {
        "prepared_context_id": prepared.prepared_context_id,
        "prepared_context_revision": prepared.revision,
        "product_id": "android-enterprise",
        "confirmed_product_id": "android-enterprise",
        "question": "Should we investigate the behavior?",
        "depth": "evaluate",
    }
    insight_payload = {
        "revision": insight.revision,
        "product_id": "android-enterprise",
        "confirmed_product_id": "android-enterprise",
        "question": "Should we change the managed-profile sharing policy?",
        "depth": "evaluate",
    }
    try:
        with TestClient(app, raise_server_exceptions=True) as client:
            monkeypatch.setattr(settings, "DECISION_PIPELINE_V2_ENABLED", False)
            blocked = client.post(
                "/decision-requests",
                json={**payload, "decision_pipeline_version": "evidence_v1"},
                headers={**headers, "Idempotency-Key": "request-evidence-disabled"},
            )
            assert blocked.status_code == 409
            assert engine.store.list_runs(limit=10) == []
            disabled_insight_request = client.post(
                f"/insights/{insight.insight_id}/decision-requests",
                json=insight_payload,
                headers={**headers, "Idempotency-Key": "insight-disabled"},
            )
            monkeypatch.setattr(settings, "DECISION_PIPELINE_V2_ENABLED", True)
            evidence = client.post(
                "/decision-requests",
                json={**payload, "decision_pipeline_version": "evidence_v1"},
                headers={**headers, "Idempotency-Key": "request-evidence-enabled"},
            )
            assert evidence.status_code == 202
            assert (
                engine.store.get_run(evidence.json()["run_id"])["decision_pipeline_version"]
                == "evidence_v1"
            )
            first_insight_request = client.post(
                f"/insights/{insight.insight_id}/decision-requests",
                json=insight_payload,
                headers={**headers, "Idempotency-Key": "insight-request"},
            )
            repeated_insight_request = client.post(
                f"/insights/{insight.insight_id}/decision-requests",
                json=insight_payload,
                headers={**headers, "Idempotency-Key": "insight-request"},
            )
            missing_prepared_context = client.post(
                "/decision-requests",
                json={
                    "product_id": "android-enterprise",
                    "question": "Can an Observe hold start a decision?",
                },
                headers={**headers, "Idempotency-Key": "missing-prepared-context"},
            )
            unconfirmed_product = client.post(
                "/decision-requests",
                json={
                    key: value for key, value in payload.items() if key != "confirmed_product_id"
                },
                headers={**headers, "Idempotency-Key": "unconfirmed-product"},
            )
            insufficient_evidence = client.post(
                "/decision-requests",
                json={
                    **payload,
                    "prepared_context_id": insufficient.prepared_context_id,
                    "prepared_context_revision": insufficient.revision,
                },
                headers={**headers, "Idempotency-Key": "insufficient-evidence"},
            )
            first = client.post("/decision-requests", json=payload, headers=headers)
            repeated = client.post("/decision-requests", json=payload, headers=headers)
    finally:
        app.dependency_overrides.clear()

    assert missing_prepared_context.status_code == 422
    assert disabled_insight_request.status_code == 503
    assert first_insight_request.status_code == 202
    assert repeated_insight_request.status_code == 200
    assert repeated_insight_request.json() == first_insight_request.json()
    insight_case = engine.store.get_decision_case(first_insight_request.json()["run_id"])
    assert insight_case is not None
    assert insight_case.insight_references == [
        InsightRevisionReference(insight_id=insight.insight_id, revision=insight.revision)
    ]
    assert unconfirmed_product.status_code == 422
    assert insufficient_evidence.status_code == 422
    assert first.status_code == 202
    assert repeated.status_code == 200
    assert repeated.json() == first.json()
    assert len(engine.store.list_runs(limit=10)) == 3
