"""Deterministic, bounded evidence preparation for personal insight jobs."""

import re

from app.models.insights import Candidate, EvidenceBundle, Passage, SourceRecord

_USABLE_STATUSES = frozenset({"ok", "fallback_summary"})
MAX_PASSAGES_PER_SOURCE = 12


def _passage_texts(material: str) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", material) if part.strip()]
    return paragraphs[:MAX_PASSAGES_PER_SOURCE] if len(paragraphs) > 1 else [material.strip()]


def prepare_evidence(
    candidate: Candidate, fetched_sources: list[SourceRecord], *, context_revision: str
) -> EvidenceBundle:
    """Select a seed plus at most three enrichments without inventing evidence.

    A missing usable body is represented by an empty bundle and a coverage gap;
    it is not converted into a synthetic source or an analysis failure.
    """
    usable = [source for source in fetched_sources if source.acquisition_status in _USABLE_STATUSES]
    selected = usable[:4]
    passages: list[Passage] = []
    gaps: list[str] = []
    for index, source in enumerate(selected):
        material = source.content or "\n".join(excerpt.text for excerpt in source.excerpts)
        if not material.strip():
            continue
        texts = _passage_texts(material)
        locator = (
            source.excerpts[0].locator if source.excerpts and source.excerpts[0].locator else "body"
        )
        for passage_index, text in enumerate(texts):
            passages.append(
                Passage(
                    passage_id=f"{source.source_id}:{passage_index}",
                    source_id=source.source_id,
                    locator=locator,
                    text=text,
                    role="seed" if index == 0 else "enrichment",
                )
            )
        if len(texts) == 1 and len(re.findall(r"\n\s*\n", material)) == 0:
            gaps.append(
                f"Source {source.source_id} is a coarse passage without paragraph boundaries."
            )
        paragraph_count = len(
            [part for part in re.split(r"\n\s*\n", material) if part.strip()]
        )
        if paragraph_count > MAX_PASSAGES_PER_SOURCE:
            gaps.append(
                f"Source {source.source_id} exceeded the passage limit; "
                "later paragraphs were omitted."
            )
    if not passages:
        gaps.append("No usable source body was supplied for this candidate.")
    return EvidenceBundle(
        candidate_id=candidate.candidate_id,
        source_ids=[source.source_id for source in selected if source.content or source.excerpts],
        passages=passages,
        coverage_gaps=gaps,
        freshness_status="unknown",
        provenance_status="attributable" if selected and all(
            source.acquisition_status == "ok" for source in selected
        ) else "unknown",
        novelty_status=(
            "duplicate"
            if any(source.candidate_id != candidate.candidate_id for source in selected)
            else "new"
        ),
        context_revision=context_revision,
    )
