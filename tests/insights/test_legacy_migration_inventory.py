"""External historical inventory stays a review-only batch until a separate import."""

import hashlib
import json

import pytest

from app.services.insight_migration import validate_external_legacy_inventory
from app.storage.insight_store import InsightStore


def _manifest():
    entries = [
        {
            "original_system": "gate0_sensing",
            "original_type": "sensing_file",
            "original_id": "source.md",
            "original_path": "/private/source.md",
            "source_hash": "a" * 64,
            "snapshot_revision": None,
            "classification": "legacy_submitted_preserve",
            "migration_state": "unreviewed",
            "notification_handling": "none",
            "llm_handling": "none",
            "cost_allowance_micros": 0,
            "alias_group_links": [],
        },
        {
            "original_system": "gate0_sensing",
            "original_type": "gate0_state_only",
            "original_id": "missing.md",
            "original_path": "/private/missing.md",
            "source_hash": None,
            "snapshot_revision": "b" * 64,
            "classification": "missing_source_skipped",
            "migration_state": "unresolved",
            "notification_handling": "none",
            "llm_handling": "none",
            "cost_allowance_micros": 0,
            "alias_group_links": [],
        },
    ]
    snapshot = {"coverage": "sensing_engine_wiki_references_with_outputs_artifacts_and_capture_rows"}
    digest = hashlib.sha256(json.dumps(
        {"entries": entries, "snapshot": snapshot}, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    return {"manifest_version": 4, "manifest_hash": digest, "entries": entries, "snapshot": snapshot}


def test_legacy_store_keeps_the_batch_inert_after_restart(tmp_path):
    database_url = f"sqlite:///{tmp_path}/insights.db"
    store = InsightStore(database_url)
    store.initialize_schema()
    manifest = _manifest()
    payload = validate_external_legacy_inventory(manifest)
    payload["inventory_id"] = "legacy-inventory-1"

    assert store.save_migration_inventory(
        payload["inventory_id"], payload["inventory_hash"], payload
    ) == payload
    restarted = InsightStore(database_url)
    assert restarted.get_migration_inventory("legacy-inventory-1") == payload
    with pytest.raises(ValueError, match="reviewed import path"):
        restarted.import_migration_inventory(
            "legacy-inventory-1", manifest["manifest_hash"], batch_size=100
        )
    with pytest.raises(ValueError, match="reviewed import path"):
        restarted.set_migration_overlay(
            "legacy-inventory-1", manifest["manifest_hash"], enabled=False
        )
    assert restarted.list_migration_aliases("legacy-inventory-1") == []


def test_legacy_batch_is_authenticated_idempotent_and_not_importable(client, auth_headers):
    payload = _manifest()
    unauthorized = client.post("/insight-migration-inventories/legacy", json=payload)
    assert unauthorized.status_code == 401

    created = client.post("/insight-migration-inventories/legacy", json=payload, headers=auth_headers)
    assert created.status_code == 201
    body = created.json()
    assert body["manifest_hash"] == payload["manifest_hash"]
    assert body["record_count"] == 2
    assert body["unresolved_count"] == 1
    assert client.post(
        "/insight-migration-inventories/legacy", json=payload, headers=auth_headers
    ).json()["inventory_id"] == body["inventory_id"]

    fetched = client.get(
        f"/insight-migration-inventories/legacy/{body['inventory_id']}", headers=auth_headers
    )
    assert fetched.status_code == 200
    assert fetched.json() == body

    rejected_import = client.post(
        f"/insight-migration-inventories/{body['inventory_id']}/import",
        json={"inventory_hash": payload["manifest_hash"], "batch_size": 100},
        headers=auth_headers,
    )
    assert rejected_import.status_code == 409
    rejected_overlay = client.post(
        f"/insight-migration-inventories/{body['inventory_id']}/overlay",
        json={"inventory_hash": payload["manifest_hash"], "enabled": False},
        headers=auth_headers,
    )
    assert rejected_overlay.status_code == 409


def test_legacy_records_are_authenticated_paged_and_filterable(client, auth_headers):
    manifest = _manifest()
    created = client.post(
        "/insight-migration-inventories/legacy", json=manifest, headers=auth_headers
    )
    assert created.status_code == 201
    inventory_id = created.json()["inventory_id"]
    url = f"/insight-migration-inventories/legacy/{inventory_id}/records"

    assert client.get(url).status_code == 401
    first = client.get(url, params={"limit": 1}, headers=auth_headers)
    assert first.status_code == 200
    assert first.json() == {
        "inventory_id": inventory_id,
        "manifest_hash": manifest["manifest_hash"],
        "record_count": 2,
        "unresolved_count": 1,
        "total_count": 2,
        "records": [manifest["entries"][0]],
        "next_offset": 1,
    }
    second = client.get(url, params={"offset": 1, "limit": 1}, headers=auth_headers)
    assert second.status_code == 200
    assert second.json()["records"] == [manifest["entries"][1]]
    assert second.json()["next_offset"] is None

    unresolved = client.get(
        url, params={"migration_state": "unresolved"}, headers=auth_headers
    )
    assert unresolved.status_code == 200
    assert unresolved.json()["total_count"] == 1
    assert unresolved.json()["records"] == [manifest["entries"][1]]
    assert client.get(url, params={"limit": 201}, headers=auth_headers).status_code == 422
    assert client.get(
        "/insight-migration-inventories/legacy/absent/records", headers=auth_headers
    ).status_code == 404


def test_legacy_batch_rejects_changed_hash_and_duplicate_origin(client, auth_headers):
    payload = _manifest()
    bad_hash = {**payload, "manifest_hash": "0" * 64}
    assert client.post(
        "/insight-migration-inventories/legacy", json=bad_hash, headers=auth_headers
    ).status_code == 422

    duplicated = _manifest()
    duplicated["entries"] = [duplicated["entries"][0], duplicated["entries"][0]]
    duplicated["manifest_hash"] = hashlib.sha256(json.dumps(
        {"entries": duplicated["entries"], "snapshot": duplicated["snapshot"]},
        sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    assert client.post(
        "/insight-migration-inventories/legacy", json=duplicated, headers=auth_headers
    ).status_code == 422
