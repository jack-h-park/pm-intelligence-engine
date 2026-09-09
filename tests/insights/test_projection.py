from app.models.insights import InsightRevision
from app.services.insight_projection import project_insight, reconcile_projections


def test_projection_is_rebuildable_and_replaces_only_its_revision_file(tmp_path):
    insight = InsightRevision(
        insight_id="insight-projection-1",
        prepared_context_id="prepared-1",
        headline="A projection fixture",
        explanation="Bounded explanation.",
        actual_change="An observed change.",
        why_now="A current reason.",
        personal_relevance="A relevant reason.",
        takeaway="A concrete takeaway.",
        claims=[{"text": "An observed change.", "passage_ids": ["passage-1"]}],
        context_revision="fixture-v1",
    )
    output = tmp_path / "outputs" / "signal-intelligence"
    output.mkdir(parents=True)
    authored = tmp_path / "wiki" / "authored.md"
    authored.parent.mkdir()
    authored.write_text("do not modify", encoding="utf-8")

    path = project_insight(insight, output)

    assert path.name == "insight-projection-1.md"
    assert "A concrete takeaway." in path.read_text(encoding="utf-8")
    assert authored.read_text(encoding="utf-8") == "do not modify"


def test_reconcile_restores_a_missing_current_projection(tmp_path):
    insight = InsightRevision(
        insight_id="insight-projection-2", prepared_context_id="prepared-2", headline="Reconcile",
        explanation="Explanation.", actual_change="Change.", why_now="Now.",
        personal_relevance="Relevant.", takeaway="Takeaway.",
        claims=[{"text": "Claim.", "passage_ids": ["passage-2"]}], context_revision="fixture-v1",
    )
    output = tmp_path / "outputs" / "signal-intelligence"

    written = reconcile_projections([insight], output)

    assert written == [output / "insight-projection-2.md"]
    assert written[0].is_file()
