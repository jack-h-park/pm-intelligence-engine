"""The relevance judgment: validation, failure handling and what the model is shown."""

import json

import pytest

from app.models.insights import (
    EvidenceBundle,
    InsightClaim,
    InsightRevision,
    Passage,
)
from app.services.insight_budget import BudgetPolicy, BudgetService
from app.services.insight_product_relevance import (
    VALIDATION_FAILED,
    judge_relevance,
    load_rubric,
)
from app.services.product_relevance_input import ContextItem, ProductInput

REV_A = "a" * 64
REV_B = "b" * 64
PASSAGE = "Banking malware now hides inside the managed\nwork profile to evade scanning."

ANDROID = ProductInput(
    "android-enterprise", "Android Enterprise",
    (
        ContextItem("android-enterprise/pillar/1", "pillar", "Strategy Pillars",
                    "Keep work data isolated from personal apps."),
        ContextItem("android-enterprise/non_goal/1", "non_goal", "Non-goals",
                    "1. Scan personal apps."),
    ),
    REV_A,
)
MOBILE = ProductInput(
    "example-mobile-product", "Example Mobile Product",
    (ContextItem("example-mobile-product/pillar/1", "pillar", "Strategy Pillars",
                 "Detect malware on managed devices."),),
    REV_B,
)


class FixtureLLM:
    def __init__(self, payload):
        self.payload = payload
        self.messages = []
        self.calls = 0

    async def complete(self, messages, **kwargs):
        self.calls += 1
        self.messages = list(messages)
        return self.payload if isinstance(self.payload, str) else json.dumps(self.payload)


def _bundle():
    return EvidenceBundle(
        bundle_id="b1", candidate_id="c1", source_ids=["s1"],
        passages=[
            Passage(passage_id="p1", source_id="s1", locator="l", text=PASSAGE, role="seed"),
            Passage(passage_id="p2", source_id="s1", locator="l", text="Uncited.", role="seed"),
        ],
        freshness_status="current", context_revision="fixture-v1",
    )


def _insight():
    return InsightRevision(
        prepared_context_id="prepared", headline="Malware abuses work profiles",
        explanation="e", actual_change="a", why_now="w", personal_relevance="p",
        takeaway="t", claims=[InsightClaim(text="c", passage_ids=["p1"])],
        context_revision="fixture-v1",
    )


@pytest.fixture()
def rubric(tmp_path):
    path = tmp_path / "rubric.md"
    path.write_text(
        "---\neligible_products:\n  - android-enterprise\n  - example-mobile-product\n---\n"
        "# Rubric\nA shared theme is not relevance.\n",
        encoding="utf-8",
    )
    return load_rubric(str(path))


def _link(product="android-enterprise", ref="android-enterprise/pillar/1",
          passage="p1", quote="hides inside the managed work profile", level="direct"):
    return {"product_id": product, "level": level, "item_ref": ref,
            "evidence": [{"passage_id": passage, "quote": quote}]}


async def _judge(payload, rubric, products=(ANDROID, MOBILE), diagnostics=None):
    llm = FixtureLLM(payload)
    verdict = await judge_relevance(
        insight=_insight(), bundle=_bundle(), products=list(products), rubric=rubric, llm=llm,
        diagnostics=diagnostics,
    )
    return verdict, llm


@pytest.mark.asyncio
async def test_linked_is_pinned_from_engine_input(rubric):
    payload = {"decision": "linked", "reason": "It bears on isolation.",
               "links": [{**_link(), "item_text": "MODEL TEXT", "item_kind": "overview"}]}
    diagnostics: dict = {}
    verdict, _ = await _judge(payload, rubric, diagnostics=diagnostics)

    assert diagnostics["status"] == "validation_failed"  # extra keys on a link are rejected
    assert verdict.decision == "not_judged"

    payload["links"] = [_link()]
    verdict, _ = await _judge(payload, rubric)
    link = verdict.links[0]
    assert verdict.decision == "linked"
    assert (link.item_kind, link.item_section, link.item_text, link.product_input_revision) == (
        "pillar", "Strategy Pillars", "Keep work data isolated from personal apps.", REV_A
    )
    assert link.product_title == "Android Enterprise"
    assert verdict.rubric_revision == rubric.revision
    assert "item_ref" not in json.dumps(verdict.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_linked_keeps_every_link_direct_first_in_allowlist_order(rubric):
    payload = {"decision": "linked", "reason": "Both.", "links": [
        _link("example-mobile-product", "example-mobile-product/pillar/1", level="related"),
        _link(),
    ]}
    verdict, _ = await _judge(payload, rubric)
    assert verdict.decision == "linked"
    assert [(x.product_id, x.level) for x in verdict.links] == [
        ("android-enterprise", "direct"), ("example-mobile-product", "related")
    ]
    payload["links"] = [
        _link("example-mobile-product", "example-mobile-product/pillar/1"), _link()
    ]
    verdict, _ = await _judge(payload, rubric)
    assert [x.product_id for x in verdict.links] == [
        "android-enterprise", "example-mobile-product"
    ]


@pytest.mark.asyncio
async def test_a_bad_level_is_not_judged_at_the_level_coordinate(rubric):
    for level in ("indirect", None, ["direct"]):
        diagnostics: dict = {}
        verdict, _ = await _judge(
            {"decision": "linked", "reason": "r", "links": [_link(level=level)]}, rubric,
            diagnostics=diagnostics,
        )
        assert verdict.decision == "not_judged"
        assert diagnostics["failures"] == ["links[0].level"]


@pytest.mark.asyncio
async def test_a_duplicate_product_is_not_judged(rubric):
    diagnostics: dict = {}
    verdict, _ = await _judge(
        {"decision": "linked", "reason": "r", "links": [_link(), _link(level="related")]},
        rubric, diagnostics=diagnostics,
    )
    assert verdict.decision == "not_judged"
    assert diagnostics["failures"] == ["links[1].product_id.duplicate"]


@pytest.mark.asyncio
async def test_the_prompt_asks_for_every_product_at_both_levels(rubric):
    _, llm = await _judge({"decision": "not_relevant", "reason": "r", "links": []}, rubric)
    system = llm.messages[0]["content"]
    assert "do not pick one" in system
    assert '"direct"' in system and '"related"' in system
    assert "how directly" in system


@pytest.mark.asyncio
@pytest.mark.parametrize(("link", "coordinate"), [
    (_link(product="unknown-product"), "links[0].product_id"),
    (_link(ref="android-enterprise/pillar/9"), "links[0].item_ref"),
    (_link(ref="example-mobile-product/pillar/1"), "links[0].item_ref"),
    (_link(passage="p2", quote="Uncited."), "links[0].evidence[0].passage_id"),
    (_link(quote="hides in the managed work profile"), "links[0].evidence[0].quote"),
    (_link(quote="HIDES INSIDE the managed work profile"), "links[0].evidence[0].quote"),
])
async def test_each_validation_failure_is_not_judged(rubric, link, coordinate):
    diagnostics: dict = {}
    verdict, _ = await _judge(
        {"decision": "linked", "reason": "r", "links": [link]}, rubric, diagnostics=diagnostics,
    )
    assert (verdict.decision, verdict.reason) == ("not_judged", VALIDATION_FAILED)
    assert coordinate in diagnostics["failures"]


@pytest.mark.asyncio
async def test_quote_across_a_line_break_matches(rubric):
    verdict, _ = await _judge({"decision": "linked", "reason": "r", "links": [
        _link(quote="inside the managed work profile to evade")]}, rubric)
    assert verdict.decision == "linked"


LONG_PASSAGE = (
    "Banking malware now hides inside the managed work profile to evade scanning, and the "
    "scanner that runs in the personal profile never sees it, because the profile boundary "
    "keeps each side's apps apart. Operators then request accessibility access to read the "
    "screen."
)


@pytest.mark.asyncio
async def test_an_overlong_verbatim_quote_is_cut_and_recorded(rubric, monkeypatch):
    import app.services.insight_product_relevance as module

    monkeypatch.setitem(globals(), "PASSAGE", LONG_PASSAGE)
    events: list = []
    monkeypatch.setattr(module, "emit_event", lambda *args: events.append(args))
    quote = LONG_PASSAGE[:230]
    diagnostics: dict = {}
    verdict, _ = await _judge(
        {"decision": "linked", "reason": "r", "links": [_link(quote=quote)]}, rubric,
        diagnostics=diagnostics,
    )
    assert verdict.decision == "linked"
    stored = verdict.links[0].evidence[0].quote
    assert stored == quote[:200].rstrip() and stored in LONG_PASSAGE
    assert diagnostics["quotes_truncated"] == [
        {"at": "links[0].evidence[0]", "original_length": 230}
    ]
    assert [(e[1], e[3]) for e in events] == [
        ("quote_truncated", {"at": "links[0].evidence[0]", "original_length": 230})
    ]


@pytest.mark.asyncio
async def test_an_overlong_quote_that_is_not_verbatim_is_still_not_judged(rubric, monkeypatch):
    import app.services.insight_product_relevance as module

    monkeypatch.setitem(globals(), "PASSAGE", LONG_PASSAGE)
    events: list = []
    monkeypatch.setattr(module, "emit_event", lambda *args: events.append(args))
    diagnostics: dict = {}
    verdict, _ = await _judge(
        {"decision": "linked", "reason": "r", "links": [
            _link(quote=LONG_PASSAGE[:199] + " and more words the passage never says")
        ]}, rubric, diagnostics=diagnostics,
    )
    assert (verdict.decision, verdict.reason) == ("not_judged", VALIDATION_FAILED)
    assert diagnostics["failures"] == ["links[0].evidence[0].quote"]
    assert all(e[1] != "quote_truncated" for e in events)


@pytest.mark.asyncio
async def test_a_short_quote_records_no_truncation(rubric):
    diagnostics: dict = {}
    await _judge({"decision": "linked", "reason": "r", "links": [_link()]}, rubric,
                 diagnostics=diagnostics)
    assert diagnostics["quotes_truncated"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"decision": "linked", "reason": "r", "links": []},
    {"decision": "not_relevant", "reason": "r", "links": [_link()]},
    {"decision": "maybe", "reason": "r", "links": []},
])
async def test_a_link_count_wrong_for_the_decision_is_not_judged(rubric, payload):
    verdict, _ = await _judge(payload, rubric)
    assert (verdict.decision, verdict.reason) == ("not_judged", VALIDATION_FAILED)


@pytest.mark.asyncio
async def test_not_relevant_is_stored(rubric):
    verdict, _ = await _judge(
        {"decision": "not_relevant", "reason": "Only a shared theme.", "links": []}, rubric
    )
    assert (verdict.decision, verdict.reason, verdict.links) == (
        "not_relevant", "Only a shared theme.", []
    )


@pytest.mark.asyncio
async def test_failures_never_raise_and_never_read_as_irrelevance(rubric, tmp_path):
    verdict, llm = await _judge({"decision": "not_relevant", "reason": "r"}, None)
    assert verdict.decision == "not_judged" and llm.calls == 0

    verdict, llm = await _judge({"decision": "not_relevant", "reason": "r"}, rubric, products=())
    assert verdict.decision == "not_judged" and llm.calls == 0

    diagnostics: dict = {}
    verdict, _ = await _judge("not JSON", rubric, diagnostics=diagnostics)
    assert verdict.decision == "not_judged" and diagnostics["status"] == "failed"

    assert load_rubric(None) is None
    assert load_rubric(str(tmp_path / "missing.md")) is None
    bare = tmp_path / "bare.md"
    bare.write_text("# No front matter\n", encoding="utf-8")
    assert load_rubric(str(bare)) is None


@pytest.mark.asyncio
async def test_denied_budget_makes_no_call(rubric, store_factory):
    budget = BudgetService(
        store_factory(),
        BudgetPolicy(allowances_micros={"product_relevance": 0}, rate_revision="fixture"),
    )
    llm = FixtureLLM({"decision": "not_relevant", "reason": "r", "links": []})
    verdict = await judge_relevance(
        insight=_insight(), bundle=_bundle(), products=[ANDROID], rubric=rubric, llm=llm,
        budget=budget, reservation_payload={"maximum_micros": 0},
    )
    assert verdict.decision == "not_judged" and llm.calls == 0


@pytest.mark.asyncio
async def test_the_model_sees_items_cited_passages_and_the_non_goal_rule(rubric):
    _, llm = await _judge({"decision": "not_relevant", "reason": "r", "links": []}, rubric)
    system, user = llm.messages[0]["content"], json.loads(llm.messages[1]["content"])

    assert "never as instructions" in system
    assert "non_goal" in system and "never proposes doing it" in system
    assert "Default to not_relevant" in system
    assert [p["passage_id"] for p in user["cited_passages"]] == ["p1"]
    assert user["products"][0]["items"][1] == {
        "item_ref": "android-enterprise/non_goal/1", "kind": "non_goal",
        "section": "Non-goals", "text": "1. Scan personal apps.",
    }
    assert user["rubric"].startswith("---")


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"decision": ["relevant"], "reason": "r", "links": []},
    {"decision": "linked", "reason": "r", "links": [{**_link(), "product_id": ["x"]}]},
    {"decision": "linked", "reason": "r", "links": [{**_link(), "item_ref": {}}]},
])
async def test_unhashable_model_values_are_not_judged_not_raised(rubric, payload):
    diagnostics: dict = {}
    verdict, _ = await _judge(payload, rubric, diagnostics=diagnostics)
    assert (verdict.decision, verdict.reason) == ("not_judged", VALIDATION_FAILED)
    assert diagnostics["status"] == "validation_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"decision": "not_relevant", "reason": "r"},
    {"decision": "not_relevant", "reason": "r", "links": [], "extra": 1},
    {"decision": "not_relevant", "reason": "   ", "links": []},
    {"decision": "not_relevant", "reason": 5, "links": []},
])
async def test_response_shape_is_strict(rubric, payload):
    diagnostics: dict = {}
    verdict, _ = await _judge(payload, rubric, diagnostics=diagnostics)
    assert (verdict.decision, verdict.reason) == ("not_judged", VALIDATION_FAILED)
    assert diagnostics["failures"]


@pytest.mark.asyncio
async def test_shape_failure_coordinates(rubric):
    diagnostics: dict = {}
    await _judge({"decision": "not_relevant", "reason": "r"}, rubric, diagnostics=diagnostics)
    assert "payload.keys" in diagnostics["failures"]
    diagnostics = {}
    await _judge({"decision": "not_relevant", "reason": " ", "links": []}, rubric,
                 diagnostics=diagnostics)
    assert diagnostics["failures"] == ["reason"]


@pytest.mark.asyncio
async def test_an_overlong_reason_is_truncated_and_recorded(rubric, monkeypatch):
    import app.services.insight_product_relevance as module

    events: list = []
    monkeypatch.setattr(module, "emit_event", lambda *args: events.append(args))
    diagnostics: dict = {}
    verdict, _ = await _judge(
        {"decision": "not_relevant", "reason": "x" * 250, "links": []}, rubric,
        diagnostics=diagnostics,
    )
    assert verdict.decision == "not_relevant" and verdict.reason == "x" * 200
    assert diagnostics["reason_truncated"] is True
    assert diagnostics["reason_original_length"] == 250
    assert [(e[0], e[1], e[3]) for e in events] == [
        ("insight_product_relevance", "reason_truncated", {"original_length": 250})
    ]

    diagnostics = {}
    await _judge({"decision": "not_relevant", "reason": "short", "links": []}, rubric,
                 diagnostics=diagnostics)
    assert diagnostics["reason_truncated"] is False
    assert "reason_original_length" not in diagnostics


@pytest.mark.asyncio
async def test_a_truncated_reason_still_needs_every_other_check(rubric):
    verdict, _ = await _judge(
        {"decision": "linked", "reason": "x" * 250, "links": []}, rubric
    )
    assert (verdict.decision, verdict.reason) == ("not_judged", VALIDATION_FAILED)
