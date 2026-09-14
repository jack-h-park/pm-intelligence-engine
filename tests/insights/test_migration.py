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


def test_migration_api_only_accepts_unreviewed_notification_free_manifest(client, auth_headers):
    payload = {
        "original_system": "gate0_sensing", "original_id": "legacy.md", "snapshot_hash": "a" * 64,
        "classification": "legacy_submitted_preserve", "migration_state": "unreviewed",
        "notification_handling": "none",
    }
    response = client.post("/insight-migrations", json=payload, headers=auth_headers)
    assert response.status_code == 201


def test_migration_store_lists_reference_and_evidence_gap_overlays(store_factory):
    store = store_factory()
    for index, classification in enumerate(("legacy_skipped_reference", "needs_evidence")):
        store.save_migration_manifest(
            {
                "original_system": "gate0_sensing",
                "original_id": f"{index}.md",
                "snapshot_hash": str(index) * 64,
                "classification": classification,
                "migration_state": "unreviewed",
                "notification_handling": "none",
            }
        )
    assert [item["classification"] for item in store.list_migration_manifests()] == [
        "legacy_skipped_reference",
        "needs_evidence",
    ]


def test_migration_api_lists_read_only_filtered_overlays(client, auth_headers):
    for index, classification in enumerate(("legacy_skipped_reference", "needs_evidence")):
        response = client.post(
            "/insight-migrations",
            json={
                "original_system": "gate0_sensing",
                "original_id": f"{index}.md",
                "snapshot_hash": str(index) * 64,
                "classification": classification,
                "migration_state": "unreviewed",
                "notification_handling": "none",
            },
            headers=auth_headers,
        )
        assert response.status_code == 201

    response = client.get(
        "/insight-migrations?classification=needs_evidence", headers=auth_headers
    )
    assert response.status_code == 200
    assert response.json()["items"] == [
        {
            "original_system": "gate0_sensing",
            "original_id": "1.md",
            "snapshot_hash": "1" * 64,
            "classification": "needs_evidence",
            "migration_state": "unreviewed",
            "notification_handling": "none",
        }
    ]
