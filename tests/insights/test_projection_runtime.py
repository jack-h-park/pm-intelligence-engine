from app.models.insights import InsightRevision
from app.services.insight_projection import reconcile_store_projections


class Store:
    def list_current_insights(self):
        return [
            InsightRevision(
                insight_id="runtime-insight", prepared_context_id="prepared", headline="Runtime",
                explanation="Explanation.", actual_change="Change.", why_now="Now.",
                personal_relevance="Relevant.", takeaway="Takeaway.",
                claims=[{"text": "Claim.", "passage_ids": ["passage"]}],
                context_revision="fixture-v1",
            )
        ]


def test_runtime_reconcile_uses_only_current_authoritative_insights(tmp_path):
    paths = reconcile_store_projections(Store(), tmp_path / "outputs" / "signal-intelligence")

    assert paths == [tmp_path / "outputs" / "signal-intelligence" / "runtime-insight.md"]
