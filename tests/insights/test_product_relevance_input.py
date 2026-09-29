"""Context items: what the relevance judgment may point at, pinned by the engine."""

import hashlib
import json

from app.services.context_loader import ContextLoader
from app.services.product_relevance_input import build_product_inputs, extract_items

CONTEXT = """# Example Mobile Product

## Product Overview
Mobile threat defense for managed fleets.

## Strategy Pillars

> Editorial note that is not an item.

### 1. Adoption

Make onboarding near-zero-touch.
It must work for existing fleets.

### 2. **Coverage**

Be a prerequisite across services.

## Target Customers
- Security teams at regulated enterprises.
- IT admins running mixed fleets.

## Non-goals
1. Rebuild the management console.
2. Replace third-party identity providers.

## Update Log
- 2026-09-01 something
"""


def test_items_by_kind_with_positional_refs():
    items = extract_items("example-mobile-product", CONTEXT)
    by_ref = {item.item_ref: item for item in items}

    assert by_ref["example-mobile-product/overview/1"].text == (
        "Mobile threat defense for managed fleets."
    )
    assert by_ref["example-mobile-product/pillar/1"].text == (
        "1. Adoption: Make onboarding near-zero-touch. It must work for existing fleets."
    )
    assert by_ref["example-mobile-product/pillar/2"].text == (
        "2. Coverage: Be a prerequisite across services."
    )
    assert by_ref["example-mobile-product/pillar/2"].section == "Strategy Pillars"
    assert by_ref["example-mobile-product/target_user/2"].text == "IT admins running mixed fleets."
    assert by_ref["example-mobile-product/non_goal/1"].kind == "non_goal"
    assert by_ref["example-mobile-product/non_goal/1"].text == (
        "1. Rebuild the management console."
    )
    assert not any("Editorial" in item.text or "2026-09-01" in item.text for item in items)


def test_item_and_section_caps():
    long_bullets = "\n".join(f"- {'x' * 700}" for _ in range(5))
    items = extract_items("p", f"# P\n\n## Strategy Pillars\n{long_bullets}\n")
    assert items[0].text.endswith("…") and len(items[0].text) == 600
    assert len(items) == 3  # 600 + 600 + 600 <= 2000; a fourth would exceed it


def test_first_item_kept_even_past_the_section_cap():
    items = extract_items("p", f"# P\n\n## Product Overview\n{'y' * 2500}\n")
    assert len(items) == 1


def test_a_product_with_no_recognized_section_yields_nothing():
    assert extract_items("p", "# P\n\n## Update Log\n- nothing\n") == []


def _write(root, product_id, text):
    directory = root / "products" / product_id
    directory.mkdir(parents=True)
    (directory / "context.md").write_text(text, encoding="utf-8")


def test_build_inputs_follows_the_allowlist_and_hashes_the_items(tmp_path):
    _write(tmp_path, "example-mobile-product", CONTEXT)
    _write(tmp_path, "android-enterprise", "# Android Enterprise\n\n## Product Overview\nA.\n")
    _write(tmp_path, "unlisted-product", "# Unlisted\n\n## Product Overview\nU.\n")
    _write(tmp_path, "empty-product", "# Empty\n\n## Update Log\n- nothing\n")

    inputs = build_product_inputs(
        ContextLoader(str(tmp_path)),
        ["example-mobile-product", "android-enterprise", "empty-product", "missing-product"],
    )

    assert [p.product_id for p in inputs] == ["example-mobile-product", "android-enterprise"]
    mobile = inputs[0]
    assert mobile.title == "Example Mobile Product"
    serialized = json.dumps(
        [[i.item_ref, i.kind, i.section, i.text] for i in mobile.items],
        separators=(",", ":"), ensure_ascii=False,
    )
    assert mobile.revision == hashlib.sha256(serialized.encode("utf-8")).hexdigest()
