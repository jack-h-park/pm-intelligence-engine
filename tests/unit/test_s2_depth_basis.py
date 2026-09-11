"""Depth must not be a restatement of relevance.

Measured on 46 decided Gate 1 runs before this change: `suggested_mode` was ~87%
predictable from `relevance_score` alone (40/46), and at relevance 3 perfectly so —
18 of 18 suggestions were `note`. The disagreement with the human concentrated exactly
where the suggestion was most deterministic (relevance 2 agreed 91%, 3 agreed 72%, 4
agreed 63%), and 11 of 13 overrides ran shallower.

The cause was structural, not wording: the relevance table and the depth table asked the
same question in the same vocabulary ("relevant", "actionable", "urgency"), and the depth
table was introduced with "After scoring relevance…". `depth_basis` splits them — it asks
what the signal CONTAINS, and the depth follows from that.
"""

from __future__ import annotations

import re

import pytest

from app.models.stages import S2OutputData
from app.stages.s2_insight import _MODE_GUIDANCE, build_insight_memo

BASES = ["no_product_surface", "trend_only", "named_gap", "options_exist", "commit_ready"]
DEPTHS = ["archive", "note", "structure", "evaluate", "decide"]


def _s2(**over):
    base = dict(
        what_changed="x", reframing="z", pillar_references=[],
        claims=[{"text": "c", "source": "signal", "grounds": []}],
        relevance_score=4, suggested_mode="note",
        suggestion_reasoning="clearly relevant, but nothing concrete named yet",
    )
    base.update(over)
    return S2OutputData(**base)


# ── the two axes must be independent ────────────────────────────────────────

def test_the_same_relevance_admits_different_depths():
    """The property the old prompt lacked. A highly relevant article that names
    nothing the product must answer is `note`; one that names a CVE is `structure`.
    Both are relevance 4."""
    trend = _s2(relevance_score=4, depth_basis="trend_only", suggested_mode="note")
    named = _s2(relevance_score=4, depth_basis="named_gap", suggested_mode="structure")

    assert trend.relevance_score == named.relevance_score
    assert trend.suggested_mode != named.suggested_mode


@pytest.mark.parametrize("basis", BASES)
def test_every_basis_is_accepted(basis):
    assert _s2(depth_basis=basis).depth_basis == basis


def test_an_unknown_basis_is_rejected():
    with pytest.raises(Exception):
        _s2(depth_basis="very_relevant")


# ── the guidance itself ─────────────────────────────────────────────────────

def test_the_basis_is_decided_before_the_depth():
    """Ordering was half the defect: 'After scoring relevance, recommend how deeply'
    made the depth a consequence of the score."""
    g = _MODE_GUIDANCE
    assert "After scoring relevance, recommend how deeply" not in g
    assert g.index("Depth basis") < g.index("Suggested depth follows from the basis")


def test_the_guidance_says_relevance_does_not_decide_depth():
    assert re.search(r"Relevance does \*\*not\*\* decide the depth", _MODE_GUIDANCE)


def test_every_basis_maps_to_exactly_one_depth_in_the_table():
    """A basis with no mapping, or two, would put the model back to guessing."""
    table = _MODE_GUIDANCE[_MODE_GUIDANCE.index("| depth_basis | suggested_mode |"):]
    for basis, depth in zip(BASES, DEPTHS):
        rows = re.findall(rf"^\|\s*{basis}\s*\|\s*(\w+)\s*\|$", table, re.M)
        assert rows == [depth], (basis, rows)


def test_a_relevant_trend_is_explicitly_allowed():
    """The combination the old prompt made feel like a contradiction, and the one
    that produced `structure -> note` overrides three times."""
    assert "highly relevant and still be `trend_only`" in " ".join(_MODE_GUIDANCE.split())


def test_the_low_relevance_floor_survives():
    """Auto-triage's cutoff still overrides the basis table — nobody should spend a
    decision on a 1-2 however concrete it is."""
    assert "scored 1–2" in _MODE_GUIDANCE and "archive" in _MODE_GUIDANCE


# ── stored output from before this change ───────────────────────────────────

def test_a_run_without_a_basis_still_renders():
    """This renderer exports historical runs. Printing a defaulted basis would read
    as a judgement the run never made, so the line is omitted instead."""
    from types import SimpleNamespace
    old = SimpleNamespace(
        relevance_score=3, suggested_mode="note", pillar_references=[],
        what_changed="x", reframing="z", suggestion_reasoning="r", claims=[],
    )
    md = build_insight_memo("T", "cat", old)

    assert "**Suggested Mode:** note" in md
    assert "Depth Basis" not in md


def test_a_run_with_a_basis_shows_it():
    md = build_insight_memo("T", "cat", _s2(depth_basis="named_gap"))
    assert "**Depth Basis:** named_gap" in md
