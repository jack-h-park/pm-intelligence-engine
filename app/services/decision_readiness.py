"""Evidence_v1 readiness findings that inform, but never replace, human routing."""

from app.models.stages import Assumption, DecisionReadiness, ReadinessFinding, S4OutputData


def assess_readiness(
    s4_output: S4OutputData,
    routing: str,
    assumptions: list[Assumption],
) -> DecisionReadiness:
    """Expose PRD grounding gaps without mutating the deterministic routing decision."""
    findings: list[ReadinessFinding] = []
    missing_evidence = [p.persona for p in s4_output.personas if not p.evidence_passage_ids]
    if missing_evidence:
        findings.append(
            ReadinessFinding(
                category="evidence",
                severity="Blocking",
                message="Missing pinned evidence for: " + ", ".join(sorted(missing_evidence)),
            )
        )
    missing_uncertainty = [p.persona for p in s4_output.personas if not p.uncertainties]
    if missing_uncertainty:
        findings.append(
            ReadinessFinding(
                category="uncertainty",
                severity="Blocking",
                message="Missing uncertainty disclosure for: "
                + ", ".join(sorted(missing_uncertainty)),
            )
        )
    matrix = s4_output.disagreement_matrix
    if matrix and matrix.material_disagreement_options:
        findings.append(
            ReadinessFinding(
                category="disagreement",
                severity="Blocking",
                message="Unresolved option disagreement: "
                + ", ".join(matrix.material_disagreement_options),
            )
        )
    blocking = [a.statement for a in assumptions if a.severity == "Blocking"]
    if blocking:
        findings.append(
            ReadinessFinding(
                category="blocking_assumption",
                severity="Blocking",
                message="Unresolved blocking assumptions: " + "; ".join(blocking),
            )
        )

    ready_for_prd = routing == "prd" and not findings
    return DecisionReadiness(
        ready_for_prd=ready_for_prd,
        provisional=not ready_for_prd,
        findings=findings,
    )
