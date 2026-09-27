"""Read-only, deterministic migration inventory; no LLM, notification, or mutation."""

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.storage.insight_store import InsightStore


LEGACY_COVERAGE = "sensing_engine_wiki_references_with_outputs_artifacts_and_capture_rows"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_LEGACY_SYSTEMS = {"gate0_sensing", "pm_engine", "pm_wiki"}
_LEGACY_DISPOSITIONS = {
    "run": "preserve_decision",
    "artifact": "preserve_artifact",
    "stage_output": "preserve_artifact",
    "signal": "preserve_source",
    "projection_reference": "rebuildable_projection",
}


@dataclass(frozen=True)
class MigrationInventory:
    """One deterministic classification of every immutable record, plus its hash.

    The hash is what an import is reconciled against: it covers the whole
    record list, so a candidate arriving mid-migration invalidates the plan
    instead of being silently imported under it. ``high_water_candidate_id``
    names the newest candidate the plan saw.
    """

    inventory_hash: str
    high_water_candidate_id: str | None
    records: list[dict[str, Any]]


def build_dry_run_inventory(store: InsightStore) -> MigrationInventory:
    """Classify current immutable records without changing candidates, gates, or delivery."""
    records: list[dict[str, Any]] = []
    candidates = store.list_candidates()
    for candidate in candidates:
        sources = store.list_sources_for_candidate(candidate.candidate_id)
        if candidate.origin == "legacy_import":
            disposition = "historical_reference"
            reason = "Already marked as a legacy import; preserve provenance without reprocessing."
        elif not sources:
            disposition = "unresolved"
            reason = "No retained source is available for reconciliation."
        elif any(source.acquisition_status == "ok" for source in sources):
            disposition = "reference"
            reason = (
                "Retained source material is available for later retrieval or "
                "explicit reconstruction."
            )
        else:
            disposition = "research_gap"
            reason = "Retained sources do not provide a successful acquisition record."
        records.append(
            {
                "original_id": candidate.candidate_id,
                "original_type": "candidate",
                "source_hashes": sorted(source.content_hash for source in sources),
                "disposition": disposition,
                "reason": reason,
                "notification_handling": "none",
                "llm_handling": "none",
            }
        )
    canonical = json.dumps(records, sort_keys=True, separators=(",", ":"))
    return MigrationInventory(
        inventory_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        high_water_candidate_id=candidates[-1].candidate_id if candidates else None,
        records=records,
    )


def validate_external_legacy_inventory(manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate the private cross-system inventory before storing a review-only batch.

    This only checks identity, provenance, and the whole-snapshot hash. It does
    not approve any disposition or make the historical records importable.
    """
    snapshot = manifest.get("snapshot")
    entries = manifest.get("entries")
    digest = manifest.get("manifest_hash")
    if manifest.get("manifest_version") != 4 or not isinstance(snapshot, dict) or (
        snapshot.get("coverage") != LEGACY_COVERAGE
    ) or not isinstance(entries, list) or not entries or len(entries) > 100_000 or (
        not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
    ):
        raise ValueError("legacy inventory has an unsupported version, coverage, or hash")
    canonical = json.dumps(
        {"entries": entries, "snapshot": snapshot}, sort_keys=True, separators=(",", ":")
    )
    if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != digest:
        raise ValueError("legacy inventory records do not match the declared manifest hash")

    identities = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("legacy inventory contains a non-object record")
        system, kind, original_id = (
            entry.get("original_system"), entry.get("original_type"), entry.get("original_id")
        )
        if not isinstance(system, str) or system not in _LEGACY_SYSTEMS or any(
            not isinstance(value, str) or not value or len(value) > 4096
            for value in (kind, original_id)
        ):
            raise ValueError("legacy inventory contains an invalid origin")
        identity = (system, kind, original_id)
        if identity in identities:
            raise ValueError("legacy inventory contains a duplicate origin")
        identities.add(identity)
        source_hash, revision = entry.get("source_hash"), entry.get("snapshot_revision")
        if (source_hash is not None and (
            not isinstance(source_hash, str) or _SHA256.fullmatch(source_hash) is None
        )) or (revision is not None and (
            not isinstance(revision, str) or not revision
        )) or (not source_hash and not revision) or any(
            not isinstance(entry.get(field), str) or not entry[field]
            for field in ("classification", "migration_state", "notification_handling")
        ):
            raise ValueError("legacy inventory record lacks provenance or classification")
        if entry["migration_state"] not in {"unreviewed", "unresolved"} or (
            entry["notification_handling"] not in {"none", "preserve_existing"}
        ) or entry.get("llm_handling") != "none" or entry.get("cost_allowance_micros") != 0:
            raise ValueError("legacy inventory cannot approve, notify, or spend")
        if any(field in entry for field in ("content", "body", "output_json", "content_md")):
            raise ValueError("legacy inventory must not copy source or output bodies")

    return {
        "kind": "legacy_external",
        "inventory_hash": digest,
        "snapshot": snapshot,
        "records": entries,
        "record_count": len(entries),
        "unresolved_count": sum(item["migration_state"] == "unresolved" for item in entries),
        "high_water_candidate_id": None,
    }


def validate_legacy_disposition_plan(
    inventory: dict[str, Any], proposed: dict[str, Any]
) -> dict[str, Any]:
    """Bind an explicit, complete disposition proposal to each frozen origin revision.

    Recording this plan does not approve it or make the external inventory
    importable through the Candidate-only path.
    """
    if inventory.get("kind") != "legacy_external":
        raise ValueError("legacy disposition plans require an external inventory")
    if proposed.get("manifest_hash") != inventory["inventory_hash"]:
        raise ValueError("legacy disposition plan has a changed manifest hash")
    reference = proposed.get("review_reference")
    decisions = proposed.get("decisions")
    if not isinstance(reference, str) or not reference.strip() or len(reference) > 2048:
        raise ValueError("legacy disposition plan needs a review reference")
    if not isinstance(decisions, list) or len(decisions) != inventory["record_count"]:
        raise ValueError("legacy disposition plan must cover every inventory record")

    by_identity = {}
    for decision in decisions:
        if not isinstance(decision, dict):
            raise ValueError("legacy disposition plan contains a non-object decision")
        identity = tuple(decision.get(field) for field in (
            "original_system", "original_type", "original_id"
        ))
        if identity in by_identity:
            raise ValueError("legacy disposition plan contains a duplicate origin")
        by_identity[identity] = decision

    bound = []
    for entry in inventory["records"]:
        identity = tuple(entry[field] for field in (
            "original_system", "original_type", "original_id"
        ))
        decision = by_identity.pop(identity, None)
        if decision is None:
            raise ValueError("legacy disposition plan omits an inventory origin")
        if decision.get("source_hash") != entry.get("source_hash") or (
            decision.get("snapshot_revision") != entry.get("snapshot_revision")
        ):
            raise ValueError("legacy disposition plan has a changed origin revision")
        disposition = decision.get("disposition")
        expected = _LEGACY_DISPOSITIONS.get(entry["original_type"], "preserve_reference")
        if entry["migration_state"] == "unresolved":
            if disposition != "defer_unresolved":
                raise ValueError("unresolved legacy origin must remain deferred")
        elif disposition not in {expected, "defer_unresolved"}:
            raise ValueError("legacy disposition conflicts with the origin type")
        suppress = decision.get("suppress_legacy_reminder", False)
        replacement = decision.get("replacement_insight_id")
        if type(suppress) is not bool:
            raise ValueError("legacy reminder suppression must be a boolean")
        if suppress and not (
            entry["original_system"] == "gate0_sensing"
            and entry["original_type"] == "sensing_file"
            and entry["migration_state"] != "unresolved"
            and disposition == "preserve_reference"
        ):
            raise ValueError("legacy reminder suppression applies only to resolved sensing files")
        if suppress:
            if not isinstance(replacement, str) or not replacement.strip():
                raise ValueError("legacy reminder suppression needs a replacement Insight ID")
            replacement_id = replacement.strip()
        if not suppress and replacement is not None:
            raise ValueError("replacement Insight ID requires legacy reminder suppression")
        row: dict[str, Any] = {
            "original_system": entry["original_system"],
            "original_type": entry["original_type"],
            "original_id": entry["original_id"],
            "source_hash": entry.get("source_hash"),
            "snapshot_revision": entry.get("snapshot_revision"),
            "disposition": disposition,
        }
        if suppress:
            row["suppress_legacy_reminder"] = True
            row["replacement_insight_id"] = replacement_id
        bound.append(row)
    if by_identity:
        raise ValueError("legacy disposition plan contains an unknown origin")

    canonical = {
        "source_inventory_id": inventory["inventory_id"],
        "source_manifest_hash": inventory["inventory_hash"],
        "review_reference": reference.strip(),
        "records": bound,
    }
    digest = hashlib.sha256(json.dumps(
        canonical, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return {
        "kind": "legacy_disposition_plan",
        "inventory_id": "",  # Assigned by the authenticated API before storage.
        "inventory_hash": digest,
        **canonical,
        "record_count": len(bound),
        "deferred_count": sum(row["disposition"] == "defer_unresolved" for row in bound),
        "suppressed_count": sum(row.get("suppress_legacy_reminder", False) for row in bound),
    }


def verify_legacy_preflight(
    receipt: dict[str, str], secret: str, *, inventory_id: str,
    manifest_hash: str, plan_hash: str, now: datetime | None = None,
) -> None:
    """Verify a short-lived receipt from the source-reading migration runner."""
    if len(secret) < 32:
        raise ValueError("legacy preflight signing secret is not configured")
    expected = {
        "inventory_id": inventory_id,
        "manifest_hash": manifest_hash,
        "plan_hash": plan_hash,
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise ValueError("legacy preflight does not name this inventory and plan")
    try:
        checked_at = datetime.fromisoformat(receipt["checked_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("legacy preflight has an invalid timestamp") from exc
    if checked_at.tzinfo is None:
        raise ValueError("legacy preflight timestamp must be timezone-aware")
    age = (now or datetime.now(UTC)) - checked_at
    if age < timedelta(seconds=-30) or age > timedelta(minutes=5):
        raise ValueError("legacy preflight has expired")
    nonce = receipt.get("nonce")
    if not isinstance(nonce, str) or re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
        raise ValueError("legacy preflight nonce is invalid")
    signed = {**expected, "checked_at": receipt["checked_at"], "nonce": nonce}
    canonical = json.dumps(signed, sort_keys=True, separators=(",", ":")).encode()
    actual = hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
    supplied = receipt.get("signature")
    if not isinstance(supplied, str) or not hmac.compare_digest(actual, supplied):
        raise ValueError("legacy preflight signature is invalid")
