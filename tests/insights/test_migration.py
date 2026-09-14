import pytest

from app.storage.insight_store import IdempotencyConflict


def test_migration_manifest_is_idempotent_and_never_touches_legacy_rows(store_factory):
    store = store_factory()
    payload = {
        "original_system": "gate0_sensing", "original_id": "2026-09-01-signal.md",
        "snapshot_hash": "a" * 64, "classification": "legacy_submitted_preserve",
        "migration_state": "unreviewed", "notification_handling": "none",
    }
    first = store.save_migration_manifest(payload)
    assert store.save_migration_manifest(payload) == first
    with pytest.raises(IdempotencyConflict):
        store.save_migration_manifest({**payload, "snapshot_hash": "b" * 64})
