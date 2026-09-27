"""External historical inventory stays a review-only batch until a separate import."""

import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta

import pytest

from app.services.insight_migration import (
    validate_external_legacy_inventory,
    validate_legacy_disposition_plan,
    verify_legacy_preflight,
)
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
            "disposition_reason": "Preserve the original Gate 0 reference.",
            "linked_signal_ids": ["signal-1"],
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


def _plan(manifest):
    return {
        "manifest_hash": manifest["manifest_hash"],
        "review_reference": "fixture-review-record",
        "decisions": [
            {
                "original_system": entry["original_system"],
                "original_type": entry["original_type"],
                "original_id": entry["original_id"],
                "source_hash": entry["source_hash"],
                "snapshot_revision": entry["snapshot_revision"],
                "disposition": "defer_unresolved" if entry["migration_state"] == "unresolved"
                else "preserve_reference",
            }
            for entry in manifest["entries"]
        ],
    }


def _preflight(inventory_id, manifest_hash, plan_hash, secret, checked_at=None):
    receipt = {
        "inventory_id": inventory_id,
        "manifest_hash": manifest_hash,
        "plan_hash": plan_hash,
        "checked_at": (checked_at or datetime.now(UTC)).isoformat(),
        "nonce": secrets.token_hex(16),
    }
    receipt["signature"] = hmac.new(
        secret.encode(),
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode(),
        hashlib.sha256,
    ).hexdigest()
    return receipt


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
    with pytest.raises(ValueError, match="dedicated legacy import path"):
        restarted.import_migration_inventory(
            "legacy-inventory-1", manifest["manifest_hash"], batch_size=100
        )
    with pytest.raises(ValueError, match="dedicated legacy import path"):
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

    overlay = client.get(
        f"/insight-migration-inventories/{body['inventory_id']}/overlay", headers=auth_headers
    )
    assert overlay.status_code == 200
    assert overlay.json() == {
        "inventory_id": body["inventory_id"],
        "inventory_hash": body["manifest_hash"],
        "inventory_kind": "legacy_external",
        "overlay_present": False,
        "enabled": False,
    }

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


def test_legacy_plan_requires_complete_revision_bound_decisions():
    manifest = _manifest()
    inventory = validate_external_legacy_inventory(manifest)
    inventory["inventory_id"] = "inventory-1"
    proposal = _plan(manifest)
    plan = validate_legacy_disposition_plan(inventory, proposal)
    assert plan["record_count"] == 2
    assert plan["deferred_count"] == 1
    assert plan["source_manifest_hash"] == manifest["manifest_hash"]

    incomplete = {**proposal, "decisions": proposal["decisions"][:1]}
    with pytest.raises(ValueError, match="every inventory record"):
        validate_legacy_disposition_plan(inventory, incomplete)
    changed = _plan(manifest)
    changed["decisions"][0]["source_hash"] = "0" * 64
    with pytest.raises(ValueError, match="changed origin revision"):
        validate_legacy_disposition_plan(inventory, changed)
    approved_missing = _plan(manifest)
    approved_missing["decisions"][1]["disposition"] = "preserve_reference"
    with pytest.raises(ValueError, match="remain deferred"):
        validate_legacy_disposition_plan(inventory, approved_missing)


def test_legacy_preflight_requires_fresh_signed_matching_receipt():
    secret = "fixture-preflight-secret-longer-than-32-characters"
    receipt = _preflight("inventory-1", "a" * 64, "b" * 64, secret)
    verify_legacy_preflight(
        receipt, secret, inventory_id="inventory-1", manifest_hash="a" * 64,
        plan_hash="b" * 64,
    )
    with pytest.raises(ValueError, match="does not name"):
        verify_legacy_preflight(
            receipt, secret, inventory_id="inventory-2", manifest_hash="a" * 64,
            plan_hash="b" * 64,
        )
    with pytest.raises(ValueError, match="expired"):
        verify_legacy_preflight(
            receipt, secret, inventory_id="inventory-1", manifest_hash="a" * 64,
            plan_hash="b" * 64, now=datetime.now(UTC) + timedelta(minutes=6),
        )
    tampered = {**receipt, "signature": "0" * 64}
    with pytest.raises(ValueError, match="signature"):
        verify_legacy_preflight(
            tampered, secret, inventory_id="inventory-1", manifest_hash="a" * 64,
            plan_hash="b" * 64,
        )


def test_legacy_plan_import_is_flagged_bounded_and_restart_safe(tmp_path):
    from config import settings

    database_url = f"sqlite:///{tmp_path}/legacy-review.db"
    store = InsightStore(database_url)
    store.initialize_schema()
    manifest = _manifest()
    inventory = validate_external_legacy_inventory(manifest)
    inventory["inventory_id"] = "inventory-1"
    store.save_migration_inventory("inventory-1", manifest["manifest_hash"], inventory)
    plan = validate_legacy_disposition_plan(inventory, _plan(manifest))
    plan["inventory_id"] = "plan-1"
    store.save_migration_inventory("plan-1", plan["inventory_hash"], plan)

    assert settings.INSIGHT_LEGACY_IMPORT_ENABLED is False
    first = store.import_legacy_disposition_plan(
        "inventory-1", manifest["manifest_hash"], "plan-1", plan["inventory_hash"],
        batch_size=1, preflight_signature="a" * 64,
    )
    assert first == {"imported_count": 1, "complete": False}
    restarted = InsightStore(database_url)
    second = restarted.import_legacy_disposition_plan(
        "inventory-1", manifest["manifest_hash"], "plan-1", plan["inventory_hash"],
        batch_size=1, preflight_signature="b" * 64,
    )
    assert second == {"imported_count": 1, "complete": True}
    assert restarted.import_legacy_disposition_plan(
        "inventory-1", manifest["manifest_hash"], "plan-1", plan["inventory_hash"],
        batch_size=1, preflight_signature="c" * 64,
    ) == {"imported_count": 0, "complete": True}
    aliases = restarted.list_migration_aliases("inventory-1")
    assert len(aliases) == 2
    assert {row["disposition"] for row in aliases} == {
        "preserve_reference", "defer_unresolved"
    }
    assert next(row for row in aliases if row["original_id"] == "source.md")[
        "linked_signal_ids"
    ] == ["signal-1"]
    assert restarted.get_migration_overlay("inventory-1") is None
    assert restarted.list_migration_manifests() == []
    with pytest.raises(ValueError, match="already used"):
        restarted.import_legacy_disposition_plan(
            "inventory-1", manifest["manifest_hash"], "plan-1", plan["inventory_hash"],
            batch_size=1, preflight_signature="a" * 64,
        )

    alternate = validate_legacy_disposition_plan(
        inventory, {**_plan(manifest), "review_reference": "different-review"}
    )
    alternate["inventory_id"] = "plan-2"
    restarted.save_migration_inventory("plan-2", alternate["inventory_hash"], alternate)
    with pytest.raises(ValueError, match="another plan"):
        restarted.import_legacy_disposition_plan(
            "inventory-1", manifest["manifest_hash"], "plan-2",
            alternate["inventory_hash"], batch_size=1,
            preflight_signature="d" * 64,
        )


def test_legacy_plan_api_requires_auth_and_import_flag(client, auth_headers, monkeypatch):
    from config import settings

    manifest = _manifest()
    inventory = client.post(
        "/insight-migration-inventories/legacy", json=manifest, headers=auth_headers
    ).json()
    base = f"/insight-migration-inventories/legacy/{inventory['inventory_id']}"
    proposal = _plan(manifest)
    assert client.post(base + "/plans", json=proposal).status_code == 401
    recorded = client.post(base + "/plans", json=proposal, headers=auth_headers)
    assert recorded.status_code == 201
    plan = recorded.json()
    assert plan["state"] == "recorded_unactivated"
    assert client.post(base + "/plans", json=proposal, headers=auth_headers).json() == plan
    assert client.get(base + "/plans/" + plan["plan_id"], headers=auth_headers).json() == plan
    generic = f"/insight-migration-inventories/{plan['plan_id']}"
    assert client.get(generic, headers=auth_headers).status_code == 404
    assert client.get(generic + "/overlay", headers=auth_headers).status_code == 404
    assert client.post(
        generic + "/import",
        json={"inventory_hash": plan["plan_hash"], "batch_size": 100},
        headers=auth_headers,
    ).status_code == 409
    assert client.post(
        generic + "/overlay",
        json={"inventory_hash": plan["plan_hash"], "enabled": True},
        headers=auth_headers,
    ).status_code == 409

    request = {
        "manifest_hash": manifest["manifest_hash"],
        "plan_id": plan["plan_id"], "plan_hash": plan["plan_hash"], "batch_size": 1,
        "preflight": _preflight(
            inventory["inventory_id"], manifest["manifest_hash"], plan["plan_hash"],
            "fixture-preflight-secret-longer-than-32-characters",
        ),
    }
    assert client.post(base + "/imports", json=request).status_code == 401
    assert client.post(base + "/imports", json=request, headers=auth_headers).status_code == 403
    monkeypatch.setattr(settings, "INSIGHT_LEGACY_IMPORT_ENABLED", True)
    assert client.post(base + "/imports", json=request, headers=auth_headers).status_code == 403
    monkeypatch.setattr(settings, "INSIGHT_LEGACY_APPROVED_PLAN_HASH", plan["plan_hash"])
    monkeypatch.setattr(
        settings, "INSIGHT_LEGACY_PREFLIGHT_SECRET",
        "fixture-preflight-secret-longer-than-32-characters",
    )
    stale = {**request, "preflight": {**request["preflight"], "manifest_hash": "0" * 64}}
    assert client.post(base + "/imports", json=stale, headers=auth_headers).status_code == 409
    first = client.post(base + "/imports", json=request, headers=auth_headers)
    assert first.status_code == 202
    assert first.json()["imported_count"] == 1
    assert first.json()["complete"] is False
    assert client.post(base + "/imports", json=request, headers=auth_headers).status_code == 409
    fresh = {**request, "preflight": _preflight(
        inventory["inventory_id"], manifest["manifest_hash"], plan["plan_hash"],
        settings.INSIGHT_LEGACY_PREFLIGHT_SECRET,
    )}
    assert client.post(base + "/imports", json=fresh, headers=auth_headers).json() == {
        "inventory_id": inventory["inventory_id"], "plan_id": plan["plan_id"],
        "imported_count": 1, "complete": True,
    }
    aliases = client.get(base + "/aliases", params={"limit": 1}, headers=auth_headers)
    assert aliases.status_code == 200
    assert aliases.json()["total_count"] == 2
    assert len(aliases.json()["aliases"]) == 1
    assert aliases.json()["next_offset"] == 1


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
