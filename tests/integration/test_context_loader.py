"""Integration tests for ContextLoader — reads from the real DECISION_SYSTEM_ROOT."""

import pytest

from app.services.context_loader import ContextLoader
from config import Settings, settings

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


def test_load_product_context_general(loader: ContextLoader):
    text = loader.load_product_context("general")
    assert text
    assert len(text) > 50


def test_load_full_context_general(loader: ContextLoader):
    ctx = loader.load_full_context("general")
    assert ctx.pm_identity
    assert ctx.company_context
    assert ctx.product_context
    assert ctx.product_id == "general"


def test_load_portfolio_profiles_real_repo(loader: ContextLoader):
    profiles = loader.load_portfolio_profiles()
    ids = [p.product_id for p in profiles]
    assert ids, "expected at least one real product profile"
    # pseudo-products are not fan-out targets
    assert "_template" not in ids
    assert "general" not in ids
    # every profile carries a usable overview
    assert all(p.overview.strip() for p in profiles)


def test_load_portfolio_profiles_exclude(loader: ContextLoader):
    full = {p.product_id for p in loader.load_portfolio_profiles()}
    victim = next(iter(full))
    reduced = {p.product_id for p in loader.load_portfolio_profiles(exclude=(victim,))}
    assert victim not in reduced
    assert reduced == full - {victim}


def test_load_portfolio_profiles_extraction_and_skips(tmp_path):
    products = tmp_path / "products"
    # Product Overview section -> used as the profile
    (products / "p1").mkdir(parents=True)
    (products / "p1" / "context.md").write_text(
        "# Product One\n\n## Product Overview\nOne does X.\n\n## Roadmap\nlater\n"
    )
    # No Product Overview -> falls back to the first section
    (products / "p2").mkdir(parents=True)
    (products / "p2" / "context.md").write_text("# Product Two\n\n## Vision\nTwo does Y.\n")
    # Pseudo-products skipped
    (products / "_template").mkdir(parents=True)
    (products / "_template" / "context.md").write_text("# T\n\n## Product Overview\nscaffold\n")
    (products / "general").mkdir(parents=True)
    (products / "general" / "context.md").write_text("# G\n\n## Product Overview\ncatch-all\n")
    # Missing context.md -> skipped
    (products / "p3").mkdir(parents=True)

    profiles = ContextLoader(str(tmp_path)).load_portfolio_profiles()
    by_id = {p.product_id: p for p in profiles}
    assert set(by_id) == {"p1", "p2"}
    assert by_id["p1"].title == "Product One"
    assert by_id["p1"].overview == "One does X."
    assert by_id["p2"].overview == "Two does Y."  # fallback to first section


def test_settings_accept_decision_context_root_alias(monkeypatch: pytest.MonkeyPatch, tmp_path):
    decision_context_root = tmp_path / "decision-context"
    monkeypatch.setenv("DECISION_CONTEXT_ROOT", str(decision_context_root))
    monkeypatch.delenv("DECISION_SYSTEM_ROOT", raising=False)

    local_settings = Settings()

    assert local_settings.DECISION_SYSTEM_ROOT == str(decision_context_root)
