"""Resolving a product by its declared id rather than by its directory name.

A product's id used to be the name of its directory, so renaming one broke
every reference already written down — a signal captured last month, a run row,
a decision-registry entry. The decision-context companion repo now declares
identity in products/<dir>/product.yaml (id, display_name, aliases). These pin
the half of that contract this repo is responsible for: a retired id still
finds its product, and what gets stored is the name the product goes by now.

The ids below are invented. A unit test needs two of them and a rename between
them; using a real product's would put an employer identifier in a public
repository for no gain.
"""

from pathlib import Path

import pytest

from app.services.context_loader import ContextLoader

CONTEXT = """# Product Context — Widget Tracker

**Product name:** Widget Tracker

## Product Overview

A product that does a thing worth describing in a sentence or two so
the portfolio profile has an overview to extract.
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "00-pm-identity.md").write_text("identity", encoding="utf-8")
    (tmp_path / "company-context.md").write_text("company", encoding="utf-8")
    d = tmp_path / "products" / "widget-tracker"
    d.mkdir(parents=True)
    (d / "context.md").write_text(CONTEXT, encoding="utf-8")
    (d / "product.yaml").write_text(
        "id: widget-tracker\ndisplay_name: Widget Tracker\naliases:\n  - legacy-widget-tool\n",
        encoding="utf-8",
    )
    return tmp_path


def test_the_current_id_resolves(root: Path):
    assert ContextLoader(str(root)).resolve_product("widget-tracker")[0] == "widget-tracker"


def test_a_retired_id_resolves_to_the_current_product(root: Path):
    canonical, directory = ContextLoader(str(root)).resolve_product("legacy-widget-tool")
    assert canonical == "widget-tracker"
    assert directory.name == "widget-tracker"


def test_a_retired_id_loads_the_context(root: Path):
    text = ContextLoader(str(root)).load_product_context("legacy-widget-tool")
    assert "Product Overview" in text


def test_a_run_from_a_retired_id_is_stored_under_the_current_one(root: Path):
    """Otherwise a rename leaves two ids in the database for one product and
    every later group-by splits."""
    ctx = ContextLoader(str(root)).load_full_context("legacy-widget-tool")
    assert ctx.product_id == "widget-tracker"
    assert ctx.product_context


def test_an_unknown_id_still_raises(root: Path):
    """Alias lookup must not turn a typo into somebody else's product."""
    with pytest.raises(FileNotFoundError):
        ContextLoader(str(root)).load_product_context("nonexistent-product-xyz")


def test_a_product_without_a_manifest_works_as_before(tmp_path: Path):
    """Most products have not been migrated, and must not need to be."""
    d = tmp_path / "products" / "legacy-product"
    d.mkdir(parents=True)
    (d / "context.md").write_text(CONTEXT, encoding="utf-8")
    loader = ContextLoader(str(tmp_path))
    assert loader.resolve_product("legacy-product")[0] == "legacy-product"
    assert loader.load_product_context("legacy-product")


def test_a_malformed_manifest_costs_the_alias_not_the_run(tmp_path: Path):
    """A broken product.yaml must never be the reason a run cannot start."""
    d = tmp_path / "products" / "p"
    d.mkdir(parents=True)
    (d / "context.md").write_text(CONTEXT, encoding="utf-8")
    (d / "product.yaml").write_text("id: [this is: not, valid\n", encoding="utf-8")
    loader = ContextLoader(str(tmp_path))
    assert loader.resolve_product("p")[0] == "p"
    assert loader.load_product_context("p")


def test_portfolio_profiles_report_the_declared_id(root: Path):
    profiles = ContextLoader(str(root)).load_portfolio_profiles()
    assert [p.product_id for p in profiles] == ["widget-tracker"]


def test_portfolio_profiles_exclude_by_declared_id(root: Path):
    """`exclude` carries the id of the origin product, which is canonical."""
    assert ContextLoader(str(root)).load_portfolio_profiles(exclude=("widget-tracker",)) == []
