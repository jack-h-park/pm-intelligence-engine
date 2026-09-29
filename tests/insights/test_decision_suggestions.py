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
    assert (result.would_suggest, result.state) == (True, "shown")
    assert [p.model_dump() for p in result.products] == [
        {"product_id": "example-mobile-product", "product_title": "Example Mobile Product",
         "level": None, "item_kind": None}]


def test_withheld_when_the_switch_does_not_allow():
    insight = _insight("i1")
    result = evaluate(insight, is_current=True, assessment=_assessment("candidates"),
                      referenced=set(), store=_Store(insight), allows=False)
    assert (result.would_suggest, result.state) == (True, "withheld")
    assert [p.product_id for p in result.products] == ["example-mobile-product"]


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


def test_a_non_goal_candidate_is_never_suggested():
    insight = _insight("i1")
    assessment = _assessment("candidates")
    assessment.candidates[0].item_kind = "non_goal"
    for allows in (True, False):
        result = evaluate(insight, is_current=True, assessment=assessment,
                          referenced=set(), store=_Store(insight), allows=allows)
        assert (result.would_suggest, result.state, result.products) == (False, "none", [])
        assert result.reason == "Links to a stated non-goal; not suggested"


def _linked(*specs):
    """specs: (product_id, level, item_kind)"""
    candidates = [SimpleNamespace(product_id=pid, product_title=pid.title(), level=level,
                                  item_kind=kind) for pid, level, kind in specs]
    return SimpleNamespace(assessment="candidates", candidates=candidates, reason="fixture linked")


def _run(assessment, allows=True):
    insight = _insight("i1")
    return evaluate(insight, is_current=True, assessment=assessment, referenced=set(),
                    store=_Store(insight), allows=allows)


def test_two_direct_links_are_shown_in_order():
    result = _run(_linked(("android-enterprise", "direct", "goal"),
                          ("example-mobile-product", "direct", "goal")))
    assert result.state == "shown"
    assert [p.product_id for p in result.products] == [
        "android-enterprise", "example-mobile-product"]
    assert result.products[0].level == "direct"


def test_direct_and_related_lists_both():
    result = _run(_linked(("android-enterprise", "direct", "goal"),
                          ("example-mobile-product", "related", "goal")))
    assert [(p.product_id, p.level) for p in result.products] == [
        ("android-enterprise", "direct"), ("example-mobile-product", "related")]


def test_only_related_links_are_never_suggested():
    result = _run(_linked(("android-enterprise", "related", "goal")))
    assert (result.would_suggest, result.state, result.reason) == (
        False, "none", "No direct product link")
    assert result.products == []


def test_a_non_goal_direct_plus_related_is_never_suggested():
    result = _run(_linked(("android-enterprise", "direct", "non_goal"),
                          ("example-mobile-product", "related", "goal")))
    assert (result.state, result.reason) == ("none", "Links to a stated non-goal; not suggested")


def test_a_non_goal_direct_plus_another_direct_is_shown():
    result = _run(_linked(("android-enterprise", "direct", "non_goal"),
                          ("example-mobile-product", "direct", "goal")))
    assert result.state == "shown"
    assert [p.item_kind for p in result.products] == ["non_goal", "goal"]


def test_several_anchor_candidates_are_not_suggested():
    assessment = _linked(("android-enterprise", None, None),
                         ("example-mobile-product", None, None))
    assert _run(assessment).state == "none"


def test_withheld_lists_the_products_for_review():
    result = _run(_linked(("android-enterprise", "direct", "goal")), allows=False)
    assert (result.would_suggest, result.state) == (True, "withheld")
    assert [p.product_id for p in result.products] == ["android-enterprise"]
