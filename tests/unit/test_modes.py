"""Mode vocabulary + legacy normalization (US-43)."""

import pytest

from app.modes import MODES, normalize_mode
from app.models.stages import S2OutputData, S7Input
from app.models.workflow import RunMode


def test_canonical_ladder():
    assert MODES == ("archive", "note", "structure", "evaluate", "decide")


@pytest.mark.parametrize("legacy,canonical", [
    ("file", "archive"), ("brief", "note"), ("opportunity", "structure"),
])
def test_legacy_aliases_normalized(legacy, canonical):
    assert normalize_mode(legacy) == canonical


@pytest.mark.parametrize("m", list(MODES) + [None])
def test_canonical_and_none_pass_through(m):
    assert normalize_mode(m) == m


def test_runmode_accepts_legacy_value():
    # in-code RunMode(<legacy>) resolves via _missing_
    assert RunMode("file") is RunMode.archive
    assert RunMode("opportunity") is RunMode.structure
    assert RunMode("decide") is RunMode.decide


def test_s2_output_normalizes_legacy_suggested_mode():
    out = S2OutputData(
        what_changed="x", reframing="y", pillar_references=[],
        relevance_explanation="z", relevance_score=2,
        suggested_mode="brief",  # legacy
        suggestion_reasoning="r",
    )
    assert out.suggested_mode == "note"


def test_s7_input_normalizes_legacy_mode():
    assert S7Input(mode="opportunity").mode == "structure"
