"""The stored relevance verdict: its shape rules and backward-compatible loading."""

import pytest
from pydantic import ValidationError

from app.models.insights import (
    InsightClaim,
    InsightRevision,
    ProductRelevance,
    RelevanceEvidence,
    RelevanceLink,
)

REV = "a" * 64


def _link(product_id="android-enterprise", kind="pillar"):
    return RelevanceLink(
        product_id=product_id,
        product_title=product_id.replace("-", " ").title(),
        item_kind=kind,
        item_section="Strategy Pillars",
        item_text="Keep work data isolated from personal apps.",
        product_input_revision=REV,
        evidence=[RelevanceEvidence(passage_id="p1", quote="work profile")],
    )


def test_relevant_needs_exactly_one_link():
    ProductRelevance(decision="relevant", reason="Bears on isolation.", links=[_link()])
    with pytest.raises(ValidationError, match="relevant"):
        ProductRelevance(decision="relevant", reason="x", links=[])
    with pytest.raises(ValidationError, match="relevant"):
        ProductRelevance(decision="relevant", reason="x", links=[_link(), _link()])


def test_ambiguous_needs_two_distinct_products():
    ProductRelevance(
        decision="ambiguous", reason="Both equally.",
        links=[_link(), _link("example-mobile-product")],
    )
    with pytest.raises(ValidationError, match="ambiguous"):
        ProductRelevance(decision="ambiguous", reason="x", links=[_link(), _link()])


@pytest.mark.parametrize("decision", ["not_relevant", "not_judged"])
def test_no_link_decisions_carry_no_links(decision):
    ProductRelevance(decision=decision, reason="No specific item.")
    with pytest.raises(ValidationError, match=decision):
        ProductRelevance(decision=decision, reason="x", links=[_link()])


def test_bounds_and_unknown_keys():
    with pytest.raises(ValidationError):
        ProductRelevance(decision="not_relevant", reason="x" * 201)
    with pytest.raises(ValidationError):
        RelevanceEvidence(passage_id="p1", quote="q" * 201)
    with pytest.raises(ValidationError):
        ProductRelevance(decision="not_relevant", reason="x", item_ref="p/pillar/1")


def test_a_stored_payload_without_the_field_loads_as_none():
    insight = InsightRevision(
        prepared_context_id="prepared", headline="h", explanation="e", actual_change="a",
        why_now="w", personal_relevance="p", takeaway="t",
        claims=[InsightClaim(text="c", passage_ids=["p1"])], context_revision="fixture-v1",
    )
    payload = insight.model_dump(mode="json")
    payload.pop("product_relevance")
    assert InsightRevision.model_validate(payload).product_relevance is None
