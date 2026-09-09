from app.models.insights import InsightRevision, PreparedContext
from app.services.insight_search import search_insights


def _saved_insight(store, candidate_payload, source_payload, bundle_payload):
    candidate = store.save_candidate(candidate_payload)
    source = store.save_source({**source_payload, "candidate_id": candidate.candidate_id})
    bundle = store.save_bundle(bundle_payload(candidate.candidate_id, source.source_id))
    prepared = store.save_prepared_context(
        PreparedContext(
            candidate_id=candidate.candidate_id,
            bundle_id=bundle.bundle_id,
            question="What changed in Android management?",
            validation_status="valid",
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )
    return store.save_insight(
        InsightRevision(
            prepared_context_id=prepared.prepared_context_id,
            headline="Android control change",
            explanation="Android management control now exposes a bounded behavior.",
            actual_change="Android added an administrative control.",
            why_now="A new release documented it.",
            personal_relevance="It informs managed-device decisions.",
            takeaway="Verify the control on a managed device.",
            claims=[{"text": "Android added a control.", "passage_ids": ["passage-fixture-1"]}],
            question_ids=["android-enterprise-isolation"],
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )


def test_search_returns_supported_insight_for_korean_query(
    store_factory, candidate_payload, source_payload, bundle_payload
):
    insight = _saved_insight(store_factory(), candidate_payload, source_payload, bundle_payload)

    results = search_insights(store_factory(), "안드로이드", expand=lambda _: ["Android"])

    assert [result.insight_id for result in results] == [insight.insight_id]


def test_search_returns_no_answer_without_a_supported_match(
    store_factory, candidate_payload, source_payload, bundle_payload
):
    _saved_insight(store_factory(), candidate_payload, source_payload, bundle_payload)

    assert search_insights(store_factory(), "unrelated quantum topic") == []
