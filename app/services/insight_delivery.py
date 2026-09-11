"""Durable delivery boundary; transport remains owned by the operations plane."""

from typing import Any

from app.storage.insight_store import InsightStore


def normalize_insight_mode(value: str | None) -> str:
    return value if value in {"legacy", "shadow", "insights"} else "legacy"


def queue_delivery(
    store: InsightStore, mode: str | None, insight_id: str, revision: int, channel: str
) -> dict[str, Any] | None:
    """Persist queue intent only in insights mode; shadow has no delivery side effect."""
    if normalize_insight_mode(mode) != "insights":
        return None
    return store.save_delivery_receipt(insight_id, revision, channel, "queued")


def confirm_delivery(
    store: InsightStore, insight_id: str, revision: int, channel: str, confirmed: bool
) -> dict[str, Any]:
    """A transport ambiguity is held as unknown instead of causing a blind resend."""
    return store.save_delivery_receipt(
        insight_id, revision, channel, "sent" if confirmed else "unknown"
    )
