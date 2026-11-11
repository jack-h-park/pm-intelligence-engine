"""Authenticated observation interface; no business or delivery state is written."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import get_engine
from app.factory import PMEngine
from app.services.signal_start_status import classify_start_status

router = APIRouter(prefix="/signal-start", tags=["signal-start"])


@router.get("/status")
async def signal_start_status(
    grace_hours: float = Query(default=1.0, ge=0, le=720, allow_inf_nan=False),
    failed_grace_hours: float = Query(default=6.0, ge=0, le=720, allow_inf_nan=False),
    signal_id: UUID | None = None,
    engine: PMEngine = Depends(get_engine),
) -> dict[str, Any]:
    try:
        rows = engine.store.signal_start_snapshot(str(signal_id) if signal_id else None)
        result = classify_start_status(
            rows,
            now=datetime.now(UTC),
            grace_hours=grace_hours,
            failed_grace_hours=failed_grace_hours,
            targeted=signal_id is not None,
        )
    except (SQLAlchemyError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Start-recovery snapshot unavailable") from exc
    if signal_id is not None and not rows:
        raise HTTPException(status_code=404, detail="Signal not found")
    return result
