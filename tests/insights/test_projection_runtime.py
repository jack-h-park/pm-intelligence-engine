import hashlib
import json

from app.models.insights import InsightRevision
from app.services.insight_projection import project_insight, reconcile_store_projections


class Store:
    def list_insights(self):
        return self.list_current_insights()

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
    manifest = json.loads((paths[0].parent / ".projection-manifest.json").read_text())
    assert manifest == {
        "schema_version": 1,
        "entries": [{
            "insight_id": "runtime-insight",
            "revision": 1,
            "sha256": hashlib.sha256(paths[0].read_bytes()).hexdigest(),
        }],
    }


def test_runtime_reconcile_retires_only_unchanged_superseded_projection(tmp_path):
    old = InsightRevision(
        insight_id="old-insight", prepared_context_id="prepared", headline="Old learning",
        explanation="Explanation.", actual_change="Old change.", why_now="Then.",
        personal_relevance="Relevant.", takeaway="Old takeaway.",
        claims=[{"text": "Old claim.", "passage_ids": ["passage"]}],
        context_revision="fixture-v1",
    )
    current = old.model_copy(update={
        "insight_id": "current-insight", "headline": "Corrected learning",
        "actual_change": "Corrected change.", "supersedes_insight_id": old.insight_id,
    })
    root = tmp_path / "outputs" / "signal-intelligence"
    generated_old = project_insight(old, root)
    authored = root / "other.md"
    authored.write_text("Keep this authored file.", encoding="utf-8")

    class CorrectedStore:
        def list_insights(self):
            return [old, current]

        def list_current_insights(self):
            return [current]

    paths = reconcile_store_projections(CorrectedStore(), root)

    assert paths == [root / "current-insight.md"]
    assert paths[0].is_file()
    assert not generated_old.exists()
    assert authored.read_text(encoding="utf-8") == "Keep this authored file."
    manifest = json.loads((root / ".projection-manifest.json").read_text())
    assert [entry["insight_id"] for entry in manifest["entries"]] == ["current-insight"]


def test_runtime_reconcile_preserves_modified_superseded_projection(tmp_path):
    old = InsightRevision(
        insight_id="old-insight", prepared_context_id="prepared", headline="Old learning",
        explanation="Explanation.", actual_change="Old change.", why_now="Then.",
        personal_relevance="Relevant.", takeaway="Old takeaway.",
        claims=[{"text": "Old claim.", "passage_ids": ["passage"]}],
        context_revision="fixture-v1",
    )
    root = tmp_path / "outputs" / "signal-intelligence"
    generated_old = project_insight(old, root)
    generated_old.write_text("Edited by a person.", encoding="utf-8")

    class StoreWithEditedProjection:
        def list_insights(self):
            return [old, old.model_copy(update={
                "insight_id": "current", "supersedes_insight_id": old.insight_id,
            })]

        def list_current_insights(self):
            return self.list_insights()[1:]

    reconcile_store_projections(StoreWithEditedProjection(), root)

    assert generated_old.read_text(encoding="utf-8") == "Edited by a person."
