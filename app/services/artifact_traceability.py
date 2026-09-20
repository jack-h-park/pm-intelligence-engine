"""Construct additive evidence_v1 artifact provenance without changing legacy artifacts."""

from typing import Any

from app.models.decision_case import DecisionCase
from app.models.stages import ArtifactTraceability, DecisionReadiness, RequirementEvidenceLink


def selected_option_from_approvals(events: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """Read the durable Gate 3 selection without changing legacy approval rows."""
    for event in reversed(events):
        if event.get("stage") != "s5" or event.get("action") not in {"confirm", "override"}:
            continue
        note = event.get("feedback_text") or ""
        fields = dict(
            part.split("=", 1) for part in note.split("; ") if "=" in part
        )
        return fields.get("selected_option"), fields.get("reason")
    return None, None


def build_artifact_traceability(
    case: DecisionCase | None,
    readiness: DecisionReadiness | None,
    requirement_links: list[RequirementEvidenceLink],
    proposed_metrics: list[str],
    approved_option: str | None = None,
    human_override_rationale: str | None = None,
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
        approved_option=approved_option,
        human_override_rationale=human_override_rationale,
        provisional=readiness.provisional if readiness else True,
        requirement_links=requirement_links,
        proposed_metrics=proposed_metrics,
    )
