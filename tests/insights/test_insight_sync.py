import hashlib
from datetime import UTC, datetime

import pytest

from app.models.insights import InsightRevision, PreparedContext
from app.services.insight_sync import InsightListCursor, decode_cursor, encode_cursor


def _save_fixture_insight(store, suffix: str, created_at: datetime):
    candidate = store.save_candidate(
        {
            "origin": "user_supplied",
            "subject": f"Incremental fixture {suffix}",
            "question_ids": ["fixture"],
            "source_ids": [],
            "policy_revision": "fixture-v1",
        }
    )
    source = store.save_source(
        {
            "candidate_id": candidate.candidate_id,
            "origin": "user_supplied",
            "content_hash": hashlib.sha256(f"Source {suffix}".encode()).hexdigest(),
            "acquisition_status": "ok",
            "retrieved_at": created_at,
            "content": f"Source {suffix}",
        }
    )
    bundle = store.save_bundle(
        {
            "candidate_id": candidate.candidate_id,
            "source_ids": [source.source_id],
            "passages": [
                {
                    "passage_id": f"passage-{suffix}",
                    "source_id": source.source_id,
                    "locator": "fixture",
                    "text": f"Source {suffix}",
                    "role": "seed",
                }
            ],
            "dates": [],
            "coverage_gaps": [],
            "freshness_status": "current",
            "provenance_status": "attributable",
            "novelty_status": "new",
            "context_revision": "fixture-v1",
        }
    )
    prepared = store.save_prepared_context(
        PreparedContext(
            candidate_id=candidate.candidate_id,
            bundle_id=bundle.bundle_id,
            question="What changed?",
            validation_status="valid",
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )
    return store.save_insight(
        InsightRevision(
            insight_id=f"insight-{suffix}",
            prepared_context_id=prepared.prepared_context_id,
            headline=f"Headline {suffix}",
            explanation="Explanation.",
            actual_change="Change.",
            why_now="Now.",
            personal_relevance="Relevant.",
            takeaway="Takeaway.",
            claims=[{"text": "Claim.", "passage_ids": [f"passage-{suffix}"]}],
            context_revision="fixture-v1",
            created_at=created_at,
        ).model_dump(mode="json")
    )


def test_list_insights_since_uses_created_at_then_id_as_a_stable_boundary(store_factory):
    store = store_factory()
    older = _save_fixture_insight(store, "older", datetime(2026, 9, 16, 0, 0, tzinfo=UTC))
    first = _save_fixture_insight(store, "a", datetime(2026, 9, 16, 1, 0, tzinfo=UTC))
    second = _save_fixture_insight(store, "b", datetime(2026, 9, 16, 1, 0, tzinfo=UTC))

    page, has_more = store.list_insights_since(older.created_at, None, limit=1)
    assert [item.insight_id for item in page] == [first.insight_id]
    assert has_more is True

    page, has_more = store.list_insights_since(
        older.created_at, (first.created_at, first.insight_id), limit=1
    )
    assert [item.insight_id for item in page] == [second.insight_id]
    assert has_more is False


def test_cursor_round_trip_preserves_the_engine_boundary():
    cursor = InsightListCursor(
        since=datetime(2026, 9, 16, 0, 0, tzinfo=UTC),
        created_at=datetime(2026, 9, 16, 1, 0, tzinfo=UTC),
        insight_id="insight-a",
    )
    assert decode_cursor(encode_cursor(cursor)) == cursor


@pytest.mark.parametrize("value", ["not-base64", "e30", "eyJpbnNpZ2h0X2lkIjoiIn0"])
def test_decode_cursor_rejects_malformed_or_incomplete_values(value):
    with pytest.raises(ValueError):
        decode_cursor(value)
