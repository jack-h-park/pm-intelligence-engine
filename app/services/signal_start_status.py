"""The Engine-owned, read-only classification of stranded/abandoned starts.

Keep inventory membership separate from grace eligibility: a fault entering a
longer grace has not resolved. No classification authorizes a workflow mutation.
"""

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

MAX_ITEMS = 10_000


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def classify_start_status(
    rows: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    grace_hours: float = 1.0,
    failed_grace_hours: float = 6.0,
    targeted: bool = False,
) -> dict[str, Any]:
    """Classify a complete store snapshot, preserving the deployed grace policy."""
    for value in (grace_hours, failed_grace_hours):
        if not math.isfinite(value) or not 0 <= value <= 720:
            raise ValueError("Invalid recovery grace policy")
    if now.tzinfo is None:
        raise ValueError("Recovery evaluation requires an aware clock")
    now = now.astimezone(UTC)
    items: list[dict[str, Any]] = []
    counts = {"stranded": 0, "abandoned": 0, "eligible": 0}
    for row in rows:
        count, failed = int(row["run_count"]), int(row["failed_count"])
        status = row["status"]
        kind = "none"
        if status in {"new", "in_run"} and count == 0:
            kind = "stranded"
        elif status == "new" and count > 0 and count == failed:
            kind = "abandoned"
        if kind == "none" and not targeted:
            continue
        anchor = row["last_attempt_at"] if kind == "abandoned" else row["ingested_at"]
        parsed = _timestamp(anchor)
        grace = failed_grace_hours if kind == "abandoned" else grace_hours
        eligible_at = parsed + timedelta(hours=grace) if parsed else None
        eligible = kind != "none" and (eligible_at is None or now >= eligible_at)
        if kind != "none":
            counts[kind] += 1
        counts["eligible"] += int(eligible)
        items.append(
            {
                "signal_id": row["signal_id"],
                "title": row["title"] or "(untitled)",
                "product_id": row["product_id"],
                "status": status,
                "ingested_at": row["ingested_at"],
                "last_attempt_at": row["last_attempt_at"],
                "run_count": count,
                "failed_count": failed,
                "classification": kind,
                "fresh": kind == "stranded",
                "eligible": eligible,
                "age_anchor": anchor,
                "age_unknown": parsed is None,
                "eligible_at": eligible_at.isoformat() if eligible_at else None,
                "expected_start_intent": "unknown",
            }
        )
        if len(items) > MAX_ITEMS:
            # Never turn a bounded result into an apparently complete empty scan.
            raise ValueError("Recovery snapshot exceeds supported response size")
    items.sort(key=lambda item: item["signal_id"])
    policy = {"grace_hours": grace_hours, "failed_grace_hours": failed_grace_hours}
    snapshot_id = hashlib.sha256(json.dumps([items, policy], sort_keys=True).encode()).hexdigest()
    return {
        "schema_version": 1,
        "policy_version": 1,
        "complete": True,
        "evaluated_at": now.isoformat(),
        "snapshot_id": snapshot_id,
        "policy": policy,
        "counts": counts,
        "items": items,
    }
