"""Opaque, Engine-owned cursors for incremental Insight retrieval."""

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class InsightListCursor:
    since: datetime | None
    created_at: datetime
    insight_id: str


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("cursor datetimes must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("cursor datetime must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("cursor datetime is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("cursor datetime must include timezone")
    return parsed.astimezone(UTC)


def encode_cursor(cursor: InsightListCursor) -> str:
    payload = {
        "since": _utc_text(cursor.since) if cursor.since is not None else None,
        "created_at": _utc_text(cursor.created_at),
        "insight_id": cursor.insight_id,
    }
    return base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).decode().rstrip("=")


def decode_cursor(value: str) -> InsightListCursor:
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("cursor is not valid base64url JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"since", "created_at", "insight_id"}:
        raise ValueError("cursor fields are invalid")
    if payload["since"] is not None and not isinstance(payload["since"], str):
        raise ValueError("cursor since is invalid")
    insight_id = payload["insight_id"]
    if not isinstance(insight_id, str) or not insight_id:
        raise ValueError("cursor insight id is invalid")
    return InsightListCursor(
        since=_parse_utc(payload["since"]) if payload["since"] is not None else None,
        created_at=_parse_utc(payload["created_at"]),
        insight_id=insight_id,
    )
