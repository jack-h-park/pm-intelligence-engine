"""Unit tests for tag normalisation (app/services/signal_tags.py).

The contract: any reasonable spelling of a label collapses to one canonical
form, and anything that would normalise to nothing raises rather than silently
disappearing.
"""

import pytest

from app.services.signal_tags import (
    MAX_TAG_LENGTH,
    TagError,
    normalize_tag,
    normalize_tags,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("regulation", "regulation"),
        ("Regulation", "regulation"),
        ("  Gate-1 Blocked  ", "gate-1-blocked"),
        ("gate_1_blocked", "gate-1-blocked"),
        ("Gate-1-Blocked", "gate-1-blocked"),
        ("high signal!!", "high-signal"),
        ("--leading-and-trailing--", "leading-and-trailing"),
        ("multi   space", "multi-space"),
    ],
)
def test_normalizes_to_kebab(raw, expected):
    assert normalize_tag(raw) == expected


def test_variant_spellings_collapse_to_one_tag():
    variants = ["Gate-1 Blocked", "gate_1_blocked", "  GATE-1-BLOCKED  "]
    assert {normalize_tag(v) for v in variants} == {"gate-1-blocked"}


@pytest.mark.parametrize("raw", ["", "   ", "!!!", "---"])
def test_empty_after_normalization_raises(raw):
    # Must raise, not return "" — a dropped tag would make the add look like it
    # worked while writing nothing.
    with pytest.raises(TagError):
        normalize_tag(raw)


def test_overlong_tag_raises():
    with pytest.raises(TagError, match="use a note"):
        normalize_tag("x" * (MAX_TAG_LENGTH + 1))


def test_batch_dedupes_preserving_first_seen_order():
    assert normalize_tags(["Beta", "alpha", "BETA", "alpha "]) == ["beta", "alpha"]
