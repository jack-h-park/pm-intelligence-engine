"""Integration tests for ContextLoader — reads from the real DECISION_SYSTEM_ROOT."""

import pytest

from app.services.context_loader import ContextLoader
from config import settings

pytestmark = pytest.mark.integration


@pytest.fixture
def loader() -> ContextLoader:
    return ContextLoader(settings.DECISION_SYSTEM_ROOT)


def test_load_pm_identity(loader: ContextLoader):
    text = loader.load_pm_identity()
    assert text
    assert "PM" in text or "Product" in text


def test_load_company_context(loader: ContextLoader):
    text = loader.load_company_context()
    assert text
    assert len(text) > 100


def test_load_product_context_known_product(loader: ContextLoader):
    text = loader.load_product_context("samsung-knox-lockdown-mode")
    assert text
    assert "Strategy Pillars" in text or "strategy" in text.lower()


def test_load_product_context_missing_product(loader: ContextLoader):
    with pytest.raises(FileNotFoundError):
        loader.load_product_context("nonexistent-product-xyz")


def test_load_full_context(loader: ContextLoader):
    ctx = loader.load_full_context("samsung-knox-lockdown-mode")
    assert ctx.pm_identity
    assert ctx.company_context
    assert ctx.product_context
    assert ctx.product_id == "samsung-knox-lockdown-mode"
