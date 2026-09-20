"""Dry-run migration inventories (E07): plan, bounded import, activation overlay.

Separate from test_migration.py, which covers the per-origin review manifests.
An inventory is the whole-record-set plan an import is reconciled against.
"""

import hashlib
from datetime import UTC, datetime

import pytest

from app.models.insights import Candidate, SourceRecord
from app.services.insight_migration import build_dry_run_inventory
from app.storage.insight_store import InsightStore


def _store(tmp_path):
    store = InsightStore(f"sqlite:///{tmp_path}/insights.db")
    store.initialize_schema()
    return store


def _candidate(store, candidate_id="candidate-1"):
    return store.save_candidate(
        Candidate(
            candidate_id=candidate_id,
            origin="user_supplied",
            subject=candidate_id,
            question_ids=["question"],
            policy_revision="v1",
        ).model_dump()
    )


def _saved_inventory(store, inventory_id="inventory-1"):
    inventory = build_dry_run_inventory(store)
    payload = {
        "inventory_id": inventory_id,
        "inventory_hash": inventory.inventory_hash,
        "high_water_candidate_id": inventory.high_water_candidate_id,
        "records": inventory.records,
    }
    store.save_migration_inventory(inventory_id, inventory.inventory_hash, payload)
    return inventory, payload


def test_dry_run_inventory_is_deterministic_and_does_not_change_records(tmp_path):
    store = _store(tmp_path)
    candidate = _candidate(store)
    content = "Retained source"
    store.save_source(
        SourceRecord(
            source_id="source-1",
            candidate_id=candidate.candidate_id,
            origin="user_supplied",
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            acquisition_status="ok",
            retrieved_at=datetime(2026, 9, 8, tzinfo=UTC),
            content=content,
        ).model_dump(mode="json")
    )

    first = build_dry_run_inventory(store)
    second = build_dry_run_inventory(store)

    assert first.inventory_hash == second.inventory_hash
    assert first.high_water_candidate_id == "candidate-1"
    assert first.records[0]["disposition"] == "reference"
    assert first.records[0]["notification_handling"] == "none"
    assert store.get_candidate("candidate-1") is not None

    payload = {
        "inventory_id": "inventory-1",
        "inventory_hash": first.inventory_hash,
        "records": first.records,
    }
    assert store.save_migration_inventory("inventory-1", first.inventory_hash, payload) == payload
    assert store.get_migration_inventory("inventory-1") == payload


def test_saving_the_same_plan_twice_replays_the_stored_inventory(tmp_path):
    store = _store(tmp_path)
    _candidate(store)
    inventory, payload = _saved_inventory(store)

    replayed = store.save_migration_inventory("inventory-2", inventory.inventory_hash, {})

    assert replayed == payload
    assert store.get_migration_inventory("inventory-2") is None


def test_import_preserves_original_records_and_is_idempotent(tmp_path):
    store = _store(tmp_path)
    candidate = _candidate(store)
    inventory, _ = _saved_inventory(store)

    first = store.import_migration_inventory(
        "inventory-1", inventory.inventory_hash, batch_size=100
    )
    second = store.import_migration_inventory(
        "inventory-1", inventory.inventory_hash, batch_size=100
    )

    assert first == {"imported_count": 1, "complete": True}
    assert second == {"imported_count": 0, "complete": True}
    assert store.get_candidate(candidate.candidate_id) == candidate
    assert store.list_migration_aliases("inventory-1") == [
        {
            "original_id": "candidate-1",
            "disposition": "unresolved",
            "notification_handling": "none",
            "llm_handling": "none",
        }
    ]


def test_import_rejects_a_changed_inventory_hash(tmp_path):
    store = _store(tmp_path)
    _saved_inventory(store)

    with pytest.raises(ValueError, match="changed"):
        store.import_migration_inventory("inventory-1", "0" * 64, batch_size=1)


def test_import_rejects_an_unknown_inventory(tmp_path):
    from app.storage.insight_store import MissingInsightRecord

    store = _store(tmp_path)

    with pytest.raises(MissingInsightRecord):
        store.import_migration_inventory("inventory-absent", "0" * 64, batch_size=1)


def test_overlay_requires_reconciled_inventory_and_retains_imports_when_disabled(tmp_path):
    store = _store(tmp_path)
    _candidate(store)
    inventory, _ = _saved_inventory(store)

    with pytest.raises(ValueError, match="reconciled"):
        store.set_migration_overlay("inventory-1", inventory.inventory_hash, enabled=True)

    store.import_migration_inventory("inventory-1", inventory.inventory_hash, batch_size=100)

    assert store.set_migration_overlay(
        "inventory-1", inventory.inventory_hash, enabled=True
    ) == {"enabled": True}
    assert store.get_migration_overlay("inventory-1") == {"enabled": True}
    assert store.set_migration_overlay(
        "inventory-1", inventory.inventory_hash, enabled=False
    ) == {"enabled": False}
    assert store.get_migration_overlay("inventory-1") == {"enabled": False}
    assert len(store.list_migration_aliases("inventory-1")) == 1


def test_import_resumes_after_store_restart_without_duplicate_aliases(tmp_path):
    database_url = f"sqlite:///{tmp_path}/insights.db"
    store = InsightStore(database_url)
    store.initialize_schema()
    for candidate_id in ("candidate-1", "candidate-2"):
        _candidate(store, candidate_id)
    inventory, _ = _saved_inventory(store)

    assert store.import_migration_inventory(
        "inventory-1", inventory.inventory_hash, batch_size=1
    ) == {"imported_count": 1, "complete": False}

    restarted = InsightStore(database_url)

    assert restarted.import_migration_inventory(
        "inventory-1", inventory.inventory_hash, batch_size=1
    ) == {"imported_count": 1, "complete": True}
    assert [
        alias["original_id"] for alias in restarted.list_migration_aliases("inventory-1")
    ] == ["candidate-1", "candidate-2"]


def test_an_inventory_does_not_disturb_the_per_origin_review_manifests(tmp_path):
    """The two migration surfaces share a concept, not a table."""
    store = _store(tmp_path)
    _candidate(store)
    manifest = store.save_migration_manifest(
        {
            "original_system": "s2k",
            "original_id": "legacy-1",
            "snapshot_hash": "a" * 64,
            "classification": "historical_reference",
            "migration_state": "unreviewed",
            "notification_handling": "none",
        }
    )
    inventory, payload = _saved_inventory(store)

    store.import_migration_inventory("inventory-1", inventory.inventory_hash, batch_size=100)

    assert store.list_migration_manifests() == [manifest]
    assert store.get_migration_inventory("inventory-1") == payload
