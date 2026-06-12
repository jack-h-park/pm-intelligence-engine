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
    category: str = "other"
    source_type: SourceType = SourceType.manual


class SignalResponse(BaseModel):
    signal_id: str
    original_product_id: Optional[str]
    title: str
    source_url: Optional[str]
    category: str
    status: str
    source_type: str
    ingested_at: str


@router.post("", response_model=SignalResponse, status_code=201)
async def create_signal(
    body: SignalCreate,
    engine: PMEngine = Depends(get_engine),
) -> SignalResponse:
    signal_id = engine.store.save_signal(
        original_product_id=body.original_product_id,
        title=body.title,
        raw_content=body.raw_content,
        source_url=body.source_url,
        category=body.category,
        source_type=body.source_type,
    )
    signal = engine.store.get_signal(signal_id)
    if signal is None:
        raise HTTPException(status_code=500, detail="Signal creation failed")
    return SignalResponse(**signal)


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
