import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_engine
from app.api.main import app
from app.models.insights import ProductRelevance, RelevanceEvidence, RelevanceLink
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


REV = "c" * 64


def _with_verdict(engine, insight, verdict):
    stored = insight.model_copy(update={"product_relevance": verdict})
    return ProductConnectionService(engine.context_loader).assess(stored, engine.insight_store)


def _link(product="android-enterprise", kind="pillar", section="Strategy Pillars",
          text="Keep work data isolated."):
    return RelevanceLink(
        product_id=product, product_title=product.replace("-", " ").title(), item_kind=kind,
        item_section=section, item_text=text, product_input_revision=REV,
        evidence=[RelevanceEvidence(passage_id="passage-connection", quote="work profile")],
    )


ANCHOR_TEXT = "A managed work profile permits a selected cross-profile interaction."


def test_relevant_verdict_becomes_the_candidate(tmp_path, engine_with_insight):
    engine, insight = engine_with_insight(tmp_path, content=ANCHOR_TEXT)
    verdict = ProductRelevance(decision="relevant", reason="It changes isolation.",
                               links=[_link()], rubric_revision=REV)
    assessment = _with_verdict(engine, insight, verdict)

    assert assessment.assessment == "candidates"
    candidate = assessment.candidates[0]
    assert (candidate.product_id, candidate.item_kind, candidate.passage_ids) == (
        "android-enterprise", "pillar", ["passage-connection"]
    )
    assert candidate.rationale == (
        "Bears on Strategy Pillars: Keep work data isolated. It changes isolation."
    )


def test_non_goal_rationale_says_it_is_not_a_planned_feature(tmp_path, engine_with_insight):
    engine, insight = engine_with_insight(tmp_path, content=ANCHOR_TEXT)
    verdict = ProductRelevance(
        decision="relevant", reason="It challenges that choice.",
        links=[_link(kind="non_goal", section="Non-goals", text="1. Scan personal apps.")],
        rubric_revision=REV,
    )
    candidate = _with_verdict(engine, insight, verdict).candidates[0]
    assert candidate.item_kind == "non_goal"
    assert candidate.rationale.startswith(
        "Bears on a stated non-goal — not a planned feature: 1. Scan personal apps."
    )


def test_ambiguous_verdict_lists_each_product(tmp_path, engine_with_insight):
    engine, insight = engine_with_insight(tmp_path, content=ANCHOR_TEXT)
    verdict = ProductRelevance(decision="ambiguous", reason="Both equally.",
                               links=[_link(), _link("example-mobile-product")],
                               rubric_revision=REV)
    assessment = _with_verdict(engine, insight, verdict)
    assert assessment.assessment == "ambiguous"
    assert [a.product_id for a in assessment.alternatives] == [
        "android-enterprise", "example-mobile-product"
    ]
    assert assessment.candidates == []


def test_ambiguous_alternative_for_a_non_goal_says_so(tmp_path, engine_with_insight):
    engine, insight = engine_with_insight(tmp_path, content=ANCHOR_TEXT)
    verdict = ProductRelevance(
        decision="ambiguous", reason="Both equally.",
        links=[_link(kind="non_goal", section="Non-goals", text="1. Scan personal apps."),
               _link("example-mobile-product")],
        rubric_revision=REV,
    )
    alternatives = _with_verdict(engine, insight, verdict).alternatives
    assert alternatives[0].reason.startswith(
        "Bears on a stated non-goal — not a planned feature: 1. Scan personal apps."
    )
    assert alternatives[1].reason.startswith("Bears on Strategy Pillars:")


@pytest.mark.parametrize(("decision", "prefix"), [
    ("not_relevant", "Only a shared theme."),
    ("not_judged", "Relevance was not judged: Only a shared theme."),
])
def test_no_link_verdicts_never_fall_back_to_anchors(
    tmp_path, engine_with_insight, decision, prefix
):
    """ANCHOR_TEXT matches two reviewed anchors; a verdict must still win."""
    engine, insight = engine_with_insight(tmp_path, content=ANCHOR_TEXT)
    verdict = ProductRelevance(decision=decision, reason="Only a shared theme.")
    assessment = _with_verdict(engine, insight, verdict)
    assert (assessment.assessment, assessment.reason) == ("no_clear_connection", prefix)


def test_editing_context_after_a_verdict_changes_nothing(tmp_path, engine_with_insight):
    engine, insight = engine_with_insight(tmp_path, content=ANCHOR_TEXT)
    verdict = ProductRelevance(decision="relevant", reason="r", links=[_link()],
                               rubric_revision=REV)
    before = _with_verdict(engine, insight, verdict)
    context = tmp_path / "decision-context" / "products" / "android-enterprise" / "context.md"
    context.write_text("# Android Enterprise\n\n## Product Overview\nRewritten.\n",
                       encoding="utf-8")
    after = _with_verdict(engine, insight, verdict)
    assert after == before
