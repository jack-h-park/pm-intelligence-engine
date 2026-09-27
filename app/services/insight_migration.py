"""Read-only, deterministic migration inventory; no LLM, notification, or mutation."""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from app.storage.insight_store import InsightStore


LEGACY_COVERAGE = "sensing_engine_wiki_references_with_outputs_artifacts_and_capture_rows"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_LEGACY_SYSTEMS = {"gate0_sensing", "pm_engine", "pm_wiki"}


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
