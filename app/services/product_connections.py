"""Read-only, evidence-anchored product connections for immutable Insights."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, Field

from app.models.insights import InsightRevision
from app.services.context_loader import ContextLoader, ProductProfile
from app.storage.insight_store import InsightStore, MissingInsightRecord


class ProductConnectionCandidate(BaseModel):
    product_id: str
    product_title: str
    confidence: Literal["medium"] = "medium"
    rationale: str
    passage_ids: list[str] = Field(min_length=1)


class ProductConnectionAlternative(BaseModel):
    product_id: str
    product_title: str
    reason: str


class ProductConnectionAssessment(BaseModel):
    insight_id: str
    revision: int
    assessment: Literal["candidates", "ambiguous", "no_clear_connection"]
    profile_revision: str
    candidates: list[ProductConnectionCandidate] = Field(default_factory=list)
    alternatives: list[ProductConnectionAlternative] = Field(default_factory=list)
    reason: str


def _normalise(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def _profile_revision(profiles: list[ProductProfile]) -> str:
    payload = [
        {
            "product_id": profile.product_id,
            "title": profile.title,
            "overview": profile.overview,
            "connection_anchors": profile.connection_anchors,
        }
        for profile in profiles
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ProductConnectionService:
    """Assess product relevance without persisting or starting workflow work."""

    def __init__(self, context_loader: ContextLoader) -> None:
        self._context_loader = context_loader

    def assess(self, insight: InsightRevision, store: InsightStore) -> ProductConnectionAssessment:
        prepared = store.get_prepared_context(insight.prepared_context_id)
        if prepared is None:
            raise MissingInsightRecord(
                f"prepared context {insight.prepared_context_id} was not found"
            )
        bundle = store.get_bundle(prepared.bundle_id)
        if bundle is None:
            raise MissingInsightRecord(f"evidence bundle {prepared.bundle_id} was not found")

        passage_text = {passage.passage_id: passage.text for passage in bundle.passages}
        cited_passages: dict[str, str] = {}
        for claim in insight.claims:
            for passage_id in claim.passage_ids:
                text = passage_text.get(passage_id)
                if text is not None:
                    cited_passages[passage_id] = text

        profiles = self._context_loader.load_portfolio_profiles()
        revision = _profile_revision(profiles)
        matches: list[tuple[ProductProfile, list[str], list[str]]] = []
        for profile in profiles:
            matched_anchor_ids: list[str] = []
            matched_passage_ids: list[str] = []
            for anchor in profile.connection_anchors:
                normalised_anchor = _normalise(anchor)
                if not normalised_anchor:
                    continue
                matching_ids = [
                    passage_id
                    for passage_id, text in cited_passages.items()
                    if normalised_anchor in _normalise(text)
                ]
                if matching_ids:
                    matched_anchor_ids.append(anchor)
                    matched_passage_ids.extend(matching_ids)
            if matched_anchor_ids:
                matches.append(
                    (profile, matched_anchor_ids, sorted(set(matched_passage_ids)))
                )

        if not matches:
            return ProductConnectionAssessment(
                insight_id=insight.insight_id,
                revision=insight.revision,
                assessment="no_clear_connection",
                profile_revision=revision,
                reason="No reviewed product connection anchor appears in a cited Insight passage.",
            )

        high_score = max(len(anchors) for _, anchors, _ in matches)
        leaders = [item for item in matches if len(item[1]) == high_score]
        if len(leaders) > 1:
            return ProductConnectionAssessment(
                insight_id=insight.insight_id,
                revision=insight.revision,
                assessment="ambiguous",
                profile_revision=revision,
                alternatives=[
                    ProductConnectionAlternative(
                        product_id=profile.product_id,
                        product_title=profile.title,
                        reason="The same number of reviewed anchors match cited Insight passages.",
                    )
                    for profile, _, _ in leaders
                ],
                reason=(
                    "The cited evidence supports multiple products equally; no primary is selected."
                ),
            )

        profile, anchors, passage_ids = leaders[0]
        anchor_text = ", ".join(f"'{anchor}'" for anchor in anchors)
        return ProductConnectionAssessment(
            insight_id=insight.insight_id,
            revision=insight.revision,
            assessment="candidates",
            profile_revision=revision,
            candidates=[
                ProductConnectionCandidate(
                    product_id=profile.product_id,
                    product_title=profile.title,
                    rationale=(
                        "Reviewed connection anchor "
                        f"{anchor_text} appears in cited Insight evidence."
                    ),
                    passage_ids=passage_ids,
                )
            ],
            alternatives=[
                ProductConnectionAlternative(
                    product_id=other.product_id,
                    product_title=other.title,
                    reason="A reviewed anchor matched fewer cited Insight passages.",
                )
                for other, other_anchors, _ in matches
                if other.product_id != profile.product_id and len(other_anchors) < high_score
            ],
            reason="A single product has the strongest reviewed evidence connection.",
        )
