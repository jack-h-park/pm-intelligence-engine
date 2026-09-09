"""Authoritative, rebuildable lexical retrieval over stored insight revisions."""

from collections.abc import Callable

from app.models.insights import InsightRevision
from app.storage.insight_store import InsightStore


def search_insights(
    store: InsightStore, query: str, *, expand: Callable[[str], list[str]] | None = None
) -> list[InsightRevision]:
    """Return only revisions whose stored text supports the query or expansion."""
    terms = [query, *(expand(query) if expand else [])]
    normalized = [term.casefold().strip() for term in terms if term.strip()]
    matches: list[InsightRevision] = []
    for insight in store.list_insights():
        corpus = "\n".join(
            [
                insight.headline,
                insight.explanation,
                insight.actual_change,
                insight.why_now,
                insight.personal_relevance,
                insight.takeaway,
                *insight.question_ids,
                *(claim.text for claim in insight.claims),
            ]
        ).casefold()
        if any(term in corpus for term in normalized):
            matches.append(insight)
    return matches
