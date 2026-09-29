from types import SimpleNamespace

import pytest

from app.services.decision_suggestions import evaluate, lineage_ids, switch_allows


def _insight(insight_id, supersedes=None, revision=1):
    return SimpleNamespace(insight_id=insight_id, supersedes_insight_id=supersedes,
                           revision=revision)


class _Store:
    def __init__(self, *insights):
        self._by_id = {i.insight_id: i for i in insights}

    def get_insight(self, insight_id):
        return self._by_id.get(insight_id)


def _assessment(kind, product="example-mobile-product", title="Example Mobile Product"):
    if kind == "candidates":
        candidates = [SimpleNamespace(product_id=product, product_title=title)]
    else:
        candidates = []
    return SimpleNamespace(assessment=kind, candidates=candidates, reason=f"fixture {kind}")


@pytest.mark.parametrize("mode,trial,v2,expected", [
    ("off", "", True, False),
    ("on", "", True, True),
    ("on", "", False, False),
    ("trial", "insight-a", True, True),
    ("trial", " insight-x , insight-a ", True, True),
    ("trial", "insight-x", True, False),
    ("trial", "", True, False),
    ("ON", "", True, True),
    ("sometimes", "", True, False),
])
def test_switch_allows(mode, trial, v2, expected):
    assert switch_allows("insight-a", mode=mode, trial_ids=trial, v2_enabled=v2) is expected


def test_lineage_walks_every_ancestor():
    old, mid, new = _insight("i1"), _insight("i2", "i1", 2), _insight("i3", "i2", 3)
    assert lineage_ids(new, _Store(old, mid, new)) == {"i1", "i2", "i3"}


def test_lineage_stops_at_a_missing_ancestor_and_a_cycle():
    a, b = _insight("a", "b"), _insight("b", "a")
    assert lineage_ids(a, _Store(a, b)) == {"a", "b"}
    orphan = _insight("c", "gone")
    assert lineage_ids(orphan, _Store(orphan)) == {"c", "gone"}


def test_one_clear_product_is_shown_when_allowed():
    insight = _insight("i1")
    result = evaluate(insight, is_current=True, assessment=_assessment("candidates"),
                      referenced=set(), store=_Store(insight), allows=True)
    assert (result.would_suggest, result.state, result.product_id) == (
        True, "shown", "example-mobile-product")


def test_withheld_when_the_switch_does_not_allow():
    insight = _insight("i1")
    result = evaluate(insight, is_current=True, assessment=_assessment("candidates"),
                      referenced=set(), store=_Store(insight), allows=False)
    assert (result.would_suggest, result.state, result.product_id) == (True, "withheld", None)


@pytest.mark.parametrize("kind", ["ambiguous", "no_clear_connection"])
def test_no_suggestion_without_one_clear_product(kind):
    insight = _insight("i1")
    result = evaluate(insight, is_current=True, assessment=_assessment(kind),
                      referenced=set(), store=_Store(insight), allows=True)
    assert (result.would_suggest, result.state) == (False, "none")


def test_an_ancestor_already_requested_blocks_a_new_revision():
    old, new = _insight("i1"), _insight("i2", "i1", 2)
    result = evaluate(new, is_current=True, assessment=_assessment("candidates"),
                      referenced={"i1"}, store=_Store(old, new), allows=True)
    assert (result.would_suggest, result.state) == (False, "none")


def test_a_superseded_revision_is_never_suggested():
    insight = _insight("i1")
    result = evaluate(insight, is_current=False, assessment=_assessment("candidates"),
                      referenced=set(), store=_Store(insight), allows=True)
    assert result.state == "none"
