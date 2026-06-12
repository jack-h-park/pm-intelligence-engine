"""Unit tests for Portfolio Triage (US-49) — cross-product relevance routing."""

import json
import pytest
from unittest.mock import AsyncMock

from app.models.stages import PortfolioTriageOutput
from app.services.context_loader import ProductProfile
from app.stages import portfolio_triage


def _profiles() -> list[ProductProfile]:
    return [
        ProductProfile(product_id="prod-a", title="A", overview="Mobile threat defense."),
        ProductProfile(product_id="prod-b", title="B", overview="Lockdown mode."),
        ProductProfile(product_id="prod-c", title="C", overview="Generative home screen."),
    ]


def _llm_returning(scores: dict[str, int]) -> AsyncMock:
    llm = AsyncMock()
    payload = {
        "products": [
            {"product_id": pid, "relevance_score": s, "reason": f"reason for {pid}"}
            for pid, s in scores.items()
        ]
    }
    llm.complete = AsyncMock(return_value=json.dumps(payload))
    return llm


async def _run(llm, threshold=4, profiles=None):
    return await portfolio_triage.run(
        signal_id="sig-1",
        title="Android 16 background API deprecation",
        summary="Some MDM background-monitoring APIs are deprecated.",
        profiles=profiles if profiles is not None else _profiles(),
        llm=llm,
        threshold=threshold,
        pm_identity="(identity)",
        framework="(triage framework)",
    )


@pytest.mark.asyncio
async def test_threshold_selects_relevant_products():
    llm = _llm_returning({"prod-a": 5, "prod-b": 4, "prod-c": 2})
    out = await _run(llm, threshold=4)
    assert isinstance(out, PortfolioTriageOutput)
    assert out.relevant_product_ids == ["prod-a", "prod-b"]
    # every profile is scored and kept in the audit trail
    assert {p.product_id for p in out.products} == {"prod-a", "prod-b", "prod-c"}


@pytest.mark.asyncio
async def test_threshold_is_engine_authoritative():
    # Same scores, stricter threshold -> fewer relevant. The LLM payload is
    # identical; only the engine-applied cutoff changes the outcome.
    llm = _llm_returning({"prod-a": 5, "prod-b": 4, "prod-c": 2})
    out = await _run(llm, threshold=5)
    assert out.relevant_product_ids == ["prod-a"]


@pytest.mark.asyncio
async def test_no_product_passes_returns_empty():
    llm = _llm_returning({"prod-a": 2, "prod-b": 1, "prod-c": 3})
    out = await _run(llm, threshold=4)
    assert out.relevant_product_ids == []


@pytest.mark.asyncio
async def test_omitted_product_is_recorded_not_dropped():
    # LLM omits prod-c entirely — it must still appear, as a non-relevant verdict.
    llm = _llm_returning({"prod-a": 5, "prod-b": 4})
    out = await _run(llm, threshold=4)
    assert {p.product_id for p in out.products} == {"prod-a", "prod-b", "prod-c"}
    prod_c = next(p for p in out.products if p.product_id == "prod-c")
    assert prod_c.relevant is False


@pytest.mark.asyncio
async def test_out_of_range_score_is_clamped():
    llm = _llm_returning({"prod-a": 9, "prod-b": 0, "prod-c": 3})
    out = await _run(llm, threshold=4)
    scores = {p.product_id: p.relevance_score for p in out.products}
    assert scores["prod-a"] == 5
    assert scores["prod-b"] == 1


@pytest.mark.asyncio
async def test_empty_profiles_skips_llm_call():
    llm = AsyncMock()
    llm.complete = AsyncMock()
    out = await _run(llm, profiles=[])
    assert out.products == []
    llm.complete.assert_not_called()
