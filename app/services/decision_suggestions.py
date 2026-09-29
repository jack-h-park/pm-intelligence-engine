"""Optional decision suggestions, derived on every read and never persisted.

A product-connection `candidates` assessment says an Insight is relevant to one product.
It does not say a decision is needed; this module only reports the relevance and whether
the release switch lets it be shown.
A link to a stated non-goal is never suggested. It never starts, counts or records anything.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

_LINEAGE_LIMIT = 100
NON_GOAL_REASON = "Links to a stated non-goal; not suggested"


class DecisionSuggestionPreview(BaseModel):
    insight_id: str
    revision: int
    would_suggest: bool
    state: Literal["shown", "withheld", "none"]
    product_id: str | None = None
    product_title: str | None = None
    reason: str


def switch_allows(insight_id: str, *, mode: str, trial_ids: str, v2_enabled: bool) -> bool:
    """Unknown modes read as off; V2 off withholds everything."""
    if not v2_enabled:
        return False
    normalized = (mode or "").strip().lower()
    if normalized == "on":
        return True
    if normalized == "trial":
        return insight_id in {item.strip() for item in (trial_ids or "").split(",") if item.strip()}
    return False


def lineage_ids(insight: Any, store: Any) -> set[str]:
    """The Insight and every ancestor reachable through `supersedes_insight_id`."""
    ids = {insight.insight_id}
    current = insight
    while current is not None and current.supersedes_insight_id and len(ids) < _LINEAGE_LIMIT:
        ancestor = current.supersedes_insight_id
        if ancestor in ids:
            break
        ids.add(ancestor)
        current = store.get_insight(ancestor)
    return ids


def evaluate(
    insight: Any,
    *,
    is_current: bool,
    assessment: Any,
    referenced: set[str],
    store: Any,
    allows: bool,
) -> DecisionSuggestionPreview:
    base = {"insight_id": insight.insight_id, "revision": insight.revision}
    if not is_current:
        return DecisionSuggestionPreview(**base, would_suggest=False, state="none",
                                         reason="Superseded revision")
    if assessment.assessment != "candidates" or len(assessment.candidates) != 1:
        return DecisionSuggestionPreview(**base, would_suggest=False, state="none",
                                         reason=assessment.reason)
    if getattr(assessment.candidates[0], "item_kind", None) == "non_goal":
        return DecisionSuggestionPreview(**base, would_suggest=False, state="none",
                                         reason=NON_GOAL_REASON)
    if lineage_ids(insight, store) & referenced:
        return DecisionSuggestionPreview(**base, would_suggest=False, state="none",
                                         reason="A decision was already requested for this Insight")
    candidate = assessment.candidates[0]
    if not allows:
        return DecisionSuggestionPreview(**base, would_suggest=True, state="withheld",
                                         reason=assessment.reason)
    return DecisionSuggestionPreview(**base, would_suggest=True, state="shown",
                                     product_id=candidate.product_id,
                                     product_title=candidate.product_title,
                                     reason=assessment.reason)
