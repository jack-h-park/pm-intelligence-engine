"""Read-only, deterministic migration inventory; no LLM, notification, or mutation."""

import hashlib
import json
from dataclasses import dataclass

from app.storage.insight_store import InsightStore


@dataclass(frozen=True)
class MigrationManifest:
    manifest_hash: str
    high_water_candidate_id: str | None
    records: list[dict]


def build_dry_run_manifest(store: InsightStore) -> MigrationManifest:
    """Classify current immutable records without changing candidates, gates, or delivery."""
    records: list[dict] = []
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
            reason = "Retained source material is available for later retrieval or explicit reconstruction."
        else:
            disposition = "research_gap"
            reason = "Retained sources do not provide a successful acquisition record."
        records.append({
            "original_id": candidate.candidate_id,
            "original_type": "candidate",
            "source_hashes": sorted(source.content_hash for source in sources),
            "disposition": disposition,
            "reason": reason,
            "notification_handling": "none",
            "llm_handling": "none",
        })
    canonical = json.dumps(records, sort_keys=True, separators=(",", ":"))
    return MigrationManifest(
        manifest_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        high_water_candidate_id=candidates[-1].candidate_id if candidates else None,
        records=records,
    )
