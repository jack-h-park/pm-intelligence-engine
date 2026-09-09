import hashlib
from datetime import UTC, datetime

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

    payload = {
        "manifest_id": "manifest-1",
        "manifest_hash": first.manifest_hash,
        "records": first.records,
    }
    assert store.save_migration_manifest("manifest-1", first.manifest_hash, payload) == payload
    assert store.get_migration_manifest("manifest-1") == payload


def test_import_preserves_original_records_and_is_idempotent(tmp_path):
    store = InsightStore(f"sqlite:///{tmp_path}/insights.db")
    store.initialize_schema()
    candidate = store.save_candidate(Candidate(
        candidate_id="candidate-1", origin="user_supplied", subject="Subject",
        question_ids=["question"], policy_revision="v1",
    ).model_dump())
    manifest = build_dry_run_manifest(store)
    payload = {
        "manifest_id": "manifest-1",
        "manifest_hash": manifest.manifest_hash,
        "high_water_candidate_id": manifest.high_water_candidate_id,
        "records": manifest.records,
    }
    store.save_migration_manifest("manifest-1", manifest.manifest_hash, payload)

    first = store.import_migration_manifest("manifest-1", manifest.manifest_hash, batch_size=100)
    second = store.import_migration_manifest("manifest-1", manifest.manifest_hash, batch_size=100)

    assert first == {"imported_count": 1, "complete": True}
    assert second == {"imported_count": 0, "complete": True}
    assert store.get_candidate(candidate.candidate_id) == candidate
    assert store.list_migration_aliases("manifest-1") == [{
        "original_id": "candidate-1",
        "disposition": "unresolved",
        "notification_handling": "none",
        "llm_handling": "none",
    }]


def test_import_rejects_a_changed_manifest_hash(tmp_path):
    store = InsightStore(f"sqlite:///{tmp_path}/insights.db")
    store.initialize_schema()
    manifest = build_dry_run_manifest(store)
    payload = {
        "manifest_id": "manifest-1", "manifest_hash": manifest.manifest_hash,
        "high_water_candidate_id": manifest.high_water_candidate_id, "records": manifest.records,
    }
    store.save_migration_manifest("manifest-1", manifest.manifest_hash, payload)

    try:
        store.import_migration_manifest("manifest-1", "0" * 64, batch_size=1)
    except ValueError as exc:
        assert "changed" in str(exc)
    else:
        raise AssertionError("expected changed manifest hash to be rejected")


def test_overlay_requires_reconciled_manifest_and_retains_imports_when_disabled(tmp_path):
    store = InsightStore(f"sqlite:///{tmp_path}/insights.db")
    store.initialize_schema()
    store.save_candidate(Candidate(
        candidate_id="candidate-1", origin="user_supplied", subject="Subject",
        question_ids=["question"], policy_revision="v1",
    ).model_dump())
    manifest = build_dry_run_manifest(store)
    payload = {
        "manifest_id": "manifest-1", "manifest_hash": manifest.manifest_hash,
        "high_water_candidate_id": manifest.high_water_candidate_id, "records": manifest.records,
    }
    store.save_migration_manifest("manifest-1", manifest.manifest_hash, payload)

    try:
        store.set_migration_overlay("manifest-1", manifest.manifest_hash, enabled=True)
    except ValueError as exc:
        assert "reconciled" in str(exc)
    else:
        raise AssertionError("expected unreconciled manifest to be rejected")

    store.import_migration_manifest("manifest-1", manifest.manifest_hash, batch_size=100)
    assert store.set_migration_overlay(
        "manifest-1", manifest.manifest_hash, enabled=True
    ) == {"enabled": True}
    assert store.set_migration_overlay(
        "manifest-1", manifest.manifest_hash, enabled=False
    ) == {"enabled": False}
    assert len(store.list_migration_aliases("manifest-1")) == 1


def test_import_resumes_after_store_restart_without_duplicate_aliases(tmp_path):
    database_url = f"sqlite:///{tmp_path}/insights.db"
    store = InsightStore(database_url)
    store.initialize_schema()
    for candidate_id in ("candidate-1", "candidate-2"):
        store.save_candidate(Candidate(
            candidate_id=candidate_id, origin="user_supplied", subject=candidate_id,
            question_ids=["question"], policy_revision="v1",
        ).model_dump())
    manifest = build_dry_run_manifest(store)
    payload = {
        "manifest_id": "manifest-1", "manifest_hash": manifest.manifest_hash,
        "high_water_candidate_id": manifest.high_water_candidate_id, "records": manifest.records,
    }
    store.save_migration_manifest("manifest-1", manifest.manifest_hash, payload)
    assert store.import_migration_manifest(
        "manifest-1", manifest.manifest_hash, batch_size=1
    ) == {"imported_count": 1, "complete": False}

    restarted = InsightStore(database_url)
    assert restarted.import_migration_manifest(
        "manifest-1", manifest.manifest_hash, batch_size=1
    ) == {"imported_count": 1, "complete": True}
    assert [alias["original_id"] for alias in restarted.list_migration_aliases("manifest-1")] == [
        "candidate-1", "candidate-2"
    ]
