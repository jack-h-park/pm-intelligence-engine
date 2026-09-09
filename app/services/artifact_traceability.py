"""Construct additive evidence_v1 artifact provenance without changing legacy artifacts."""

from app.models.decision_case import DecisionCase
from app.models.stages import ArtifactTraceability, DecisionReadiness, RequirementEvidenceLink


def build_artifact_traceability(
    case: DecisionCase | None,
    readiness: DecisionReadiness | None,
    requirement_links: list[RequirementEvidenceLink],
    proposed_metrics: list[str],
) -> ArtifactTraceability | None:
    if case is None:
        return None
    allowed_passage_ids = {
        passage_id for fact in case.confirmed_facts for passage_id in fact.passage_ids
    }
    for link in requirement_links:
        if set(link.evidence_passage_ids) - allowed_passage_ids:
            raise ValueError("artifact references evidence outside the pinned decision case")
    return ArtifactTraceability(
        decision_case_id=case.case_id,
        decision_case_revision=case.revision,
        approved_option=None,
        provisional=readiness.provisional if readiness else True,
        requirement_links=requirement_links,
        proposed_metrics=proposed_metrics,
    )
