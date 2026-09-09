from datetime import UTC, datetime
import hashlib

from app.models.insights import Candidate, SourceRecord
from app.services.insight_migration import build_dry_run_manifest
from app.storage.insight_store import InsightStore


def test_dry_run_manifest_is_deterministic_and_does_not_change_records(tmp_path):
    store = InsightStore(f"sqlite:///{tmp_path}/insights.db")
    store.initialize_schema()
    candidate = store.save_candidate(Candidate(
        candidate_id="candidate-1", origin="user_supplied", subject="Subject",
        question_ids=["question"], policy_revision="v1",
    ).model_dump())
    content = "Retained source"
    store.save_source(SourceRecord(
        source_id="source-1", candidate_id=candidate.candidate_id, origin="user_supplied",
        content_hash=hashlib.sha256(content.encode()).hexdigest(), acquisition_status="ok",
        retrieved_at=datetime(2026, 9, 8, tzinfo=UTC), content=content,
    ).model_dump(mode="json"))

    first = build_dry_run_manifest(store)
    second = build_dry_run_manifest(store)

    assert first.manifest_hash == second.manifest_hash
    assert first.high_water_candidate_id == "candidate-1"
    assert first.records[0]["disposition"] == "reference"
    assert first.records[0]["notification_handling"] == "none"
    assert store.get_candidate("candidate-1") is not None

    payload = {"manifest_id": "manifest-1", "manifest_hash": first.manifest_hash, "records": first.records}
    assert store.save_migration_manifest("manifest-1", first.manifest_hash, payload) == payload
    assert store.get_migration_manifest("manifest-1") == payload
