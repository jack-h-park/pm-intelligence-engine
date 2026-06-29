import json
import os
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.factory import PMEngine
from app.api.deps import get_engine
from app.models.workflow import SourceType

router = APIRouter(prefix="/signals", tags=["signals"])


class SignalCreate(BaseModel):
    # `original_product_id` is the canonical field (US-49) — an optional origin
    # hint, NULL for product-agnostic intake. `product_id` is still accepted as a
    # deprecated alias so existing clients (Hermes) keep working.
    model_config = ConfigDict(populate_by_name=True)
    original_product_id: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("original_product_id", "product_id")
    )
    title: str
    raw_content: str
    source_url: Optional[str] = None
    # Optional provenance back-link to the originating intake artifact — the
    # Hermes sensing filename. Gate 0 submit passes it so the engine signal can
    # be paired with its sensing file deterministically (no fuzzy title match).
    source_ref: Optional[str] = None
    category: str = "other"
    source_type: SourceType = SourceType.manual


class SignalResponse(BaseModel):
    signal_id: str
    original_product_id: Optional[str]
    title: str
    source_url: Optional[str]
    source_ref: Optional[str] = None
    category: str
    status: str
    source_type: str
    ingested_at: str


def _check_gate0_skip(source_ref: str) -> None:
    """Raise 409 if source_ref is in the gate0-state.json `skipped` bucket.

    Called only when GATE0_STATE_FILE is configured. Any I/O or parse error is
    treated permissively — the check must never block a legitimate submission
    because of a transient read failure.
    """
    from config import settings

    state_path = settings.GATE0_STATE_FILE
    if not state_path:
        return
    try:
        with open(state_path) as fh:
            state = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return  # missing or malformed → permissive
    if not isinstance(state, dict):
        return
    skipped = state.get("skipped")
    if not isinstance(skipped, dict):
        return
    entry = skipped.get(source_ref)
    if entry is None:
        return
    reason = entry.get("reason", "") if isinstance(entry, dict) else ""
    detail = f"Signal source '{source_ref}' is in the Gate 0 skipped bucket and cannot be re-ingested."
    if reason:
        detail += f" Reason: {reason}"
    raise HTTPException(status_code=409, detail=detail)


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_URL_RE = re.compile(r"^\s*url:\s*(.+?)\s*$", re.MULTILINE)


def _sensing_source_url(source_ref: str) -> Optional[str]:
    """Recover source_url from a sensing file's YAML frontmatter ``url:`` field.

    Gate 0's ``POST /signals`` body is composed by an LLM (the gate0-signal-intake
    skill), which is instructed to copy the sensing file's frontmatter ``url`` into
    ``source_url`` — but being LLM-driven it occasionally omits it, and signals
    ingested before that instruction landed carry no source_url at all. The URL is
    always present on disk in the sensing file, so when a caller supplies
    ``source_ref`` (the deterministic sensing filename) but no ``source_url``,
    recover it here so the provenance link is never silently lost.

    Best-effort by design: a traversal-looking ref, a missing/unreadable file, or
    absent frontmatter all degrade to None rather than failing the submission.
    """
    from config import settings

    if not source_ref or "/" in source_ref or "\\" in source_ref or ".." in source_ref:
        return None
    path = Path(settings.WIKI_ROOT) / "raw" / "from-web" / "sensing" / source_ref
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    fm = _FRONTMATTER_RE.match(text)
    if not fm:
        return None
    m = _URL_RE.search(fm.group(1))
    if not m:
        return None
    url = m.group(1).strip().strip('"').strip("'")
    if not url or url.lower() in ("null", "none", "~"):
        return None
    return url


@router.post("", response_model=SignalResponse, status_code=201)
async def create_signal(
    body: SignalCreate,
    engine: PMEngine = Depends(get_engine),
) -> SignalResponse:
    if body.source_ref:
        _check_gate0_skip(body.source_ref)
    # Backfill the live source URL from the sensing file when the (LLM-composed)
    # submit omitted it but gave us the deterministic source_ref to find it by.
    source_url = body.source_url
    if not source_url and body.source_ref:
        source_url = _sensing_source_url(body.source_ref)
    signal_id = engine.store.save_signal(
        original_product_id=body.original_product_id,
        title=body.title,
        raw_content=body.raw_content,
        source_url=source_url,
        category=body.category,
        source_type=body.source_type,
        source_ref=body.source_ref,
    )
    signal = engine.store.get_signal(signal_id)
    if signal is None:
        raise HTTPException(status_code=500, detail="Signal creation failed")
    return SignalResponse(**signal)


class ReconcileResponse(BaseModel):
    """Result of a signal-status reconciliation sweep."""
    checked: int          # signals examined
    corrected: int        # signals whose status was changed
    changes: list[dict]   # [{signal_id, title, old, new}] per corrected signal


@router.post("/reconcile", response_model=ReconcileResponse)
async def reconcile_signal_statuses(
    engine: PMEngine = Depends(get_engine),
) -> ReconcileResponse:
    """Re-derive every signal's status from its runs, fixing any divergence.

    A signal's status (``new`` · ``in_run`` · ``done`` · ``blocked``) is a
    deterministic function of its runs. Rows can drift when a run is created
    outside ``finalize_run`` (e.g. a synthetic/backfilled ``completed`` row),
    leaving a signal stuck at ``in_run`` despite a completed run. This endpoint
    recomputes and repairs them — idempotent, so it is safe to re-run.
    """
    from app.services.signal_status import reconcile_all_signals

    changes = reconcile_all_signals(engine)
    checked = len(engine.store.list_signals(limit=100000))
    return ReconcileResponse(
        checked=checked, corrected=len(changes), changes=changes
    )


@router.get("", response_model=list[SignalResponse])
async def list_signals(
    # Query param kept as `product_id` for back-compat; filters on the signal's
    # origin product (original_product_id).
    product_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    engine: PMEngine = Depends(get_engine),
) -> list[SignalResponse]:
    signals = engine.store.list_signals(
        original_product_id=product_id, status=status, limit=limit
    )
    return [SignalResponse(**s) for s in signals]


@router.get("/{signal_id}", response_model=SignalResponse)
async def get_signal(
    signal_id: str,
    engine: PMEngine = Depends(get_engine),
) -> SignalResponse:
    signal = engine.store.get_signal(signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail="Signal not found")
    return SignalResponse(**signal)
