from app.models.insights import InsightRevision, PreparedContext
from app.services.insight_search import search_expansion_hash, search_insights


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


def test_search_matches_words_across_a_supported_insight_without_phrase_order(
    store_factory, candidate_payload, source_payload, bundle_payload
):
    store = store_factory()
    insight = _saved_insight(store, candidate_payload, source_payload, bundle_payload)

    assert [item.insight_id for item in search_insights(store, "Android added control")] == [
        insight.insight_id
    ]
    assert [item.insight_id for item in search_insights(store, "managed devices control")] == [
        insight.insight_id
    ]
    assert search_insights(store, "Android unrelated control") == []


def test_correction_replaces_current_search_result_but_preserves_old_record(
    store_factory, candidate_payload, source_payload, bundle_payload
):
    store = store_factory()
    old = _saved_insight(store, candidate_payload, source_payload, bundle_payload)
    corrected_payload = old.model_dump(mode="json")
    corrected_payload.update({
        "insight_id": "corrected-profile-boundary",
        "headline": "Corrected profile boundary",
        "explanation": "The profile boundary has narrower scope.",
        "actual_change": "The profile boundary changed.",
        "takeaway": "Review profile behavior.",
        "claims": [{
            "text": "The profile boundary is narrower.", "passage_ids": ["passage-fixture-1"],
        }],
        "supersedes_insight_id": old.insight_id,
    })
    corrected = store.save_insight(corrected_payload)

    assert search_insights(store_factory(), "administrative") == []
    assert [item.insight_id for item in search_insights(store_factory(), "profile boundary")] == [
        corrected.insight_id
    ]
    assert store_factory().get_insight(old.insight_id) == old


def test_expansion_claim_survives_restart_and_never_replays_an_ambiguous_call(store_factory):
    store = store_factory()
    completed_hash = search_expansion_hash("안드로이드")
    unknown_hash = search_expansion_hash("휴대폰")
    assert completed_hash == search_expansion_hash(" 안드로이드 ")

    assert store.claim_search_expansion(completed_hash) == ("claimed", None)
    assert store.claim_search_expansion(completed_hash) == ("running", None)
    store.complete_search_expansion(completed_hash, ["Android"])
    assert store_factory().claim_search_expansion(completed_hash) == (
        "complete", ["Android"]
    )

    assert store.claim_search_expansion(unknown_hash) == ("claimed", None)
    store.mark_search_expansion_unknown(unknown_hash)
    assert store_factory().claim_search_expansion(unknown_hash) == (
        "terminal_unknown", None
    )
