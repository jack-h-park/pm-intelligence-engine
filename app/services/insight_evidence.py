"""Deterministic, bounded evidence preparation for personal insight jobs."""

from app.models.insights import Candidate, EvidenceBundle, Passage, SourceRecord

_USABLE_STATUSES = frozenset({"ok", "fallback_summary"})


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
    for index, source in enumerate(selected):
        material = source.content or "\n".join(excerpt.text for excerpt in source.excerpts)
        if not material.strip():
            continue
        passages.append(
            Passage(
                passage_id=f"{source.source_id}:0",
                source_id=source.source_id,
                locator=(
                    source.excerpts[0].locator
                    if source.excerpts and source.excerpts[0].locator
                    else "body"
                ),
                text=material.strip(),
                role="seed" if index == 0 else "enrichment",
            )
        )
    gaps: list[str] = []
    if not passages:
        gaps.append("No usable source body was supplied for this candidate.")
    elif len(passages) == 1:
        gaps.append("Only one attributable source was available.")
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
