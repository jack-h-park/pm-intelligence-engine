from app.models.stages import Assumption, PersonaOutput, S4OutputData, S4RubricResult
from app.services.disagreement_matrix import build_disagreement_matrix
from app.services.decision_readiness import assess_readiness


def _s4(*, missing_evidence: bool = False, disagreement: bool = False) -> S4OutputData:
    personas = []
    for name in ("explorer", "strategist", "builder", "skeptic"):
        position = "oppose" if disagreement and name == "skeptic" else "support"
        personas.append(
            PersonaOutput(
                persona=name,  # type: ignore[arg-type]
                dimension=name,
                score=4,
                key_argument=f"{name} reasoning.",
                open_question="What customer interview would resolve this?",
                evidence_passage_ids=[] if missing_evidence and name == "skeptic" else ["passage-1"],
                uncertainties=["A customer interview could change this judgment."],
                option_positions={"Pilot": position},
            )
        )
    return S4OutputData(
        personas=personas,
        rubric=S4RubricResult(
            total_score=12,
            score_grounding=3,
            skeptic_quality=3,
            open_question_quality=3,
            persona_independence=3,
            passed=True,
        ),
        disagreement_matrix=build_disagreement_matrix(personas),
    )


def test_prd_readiness_discloses_evidence_gaps_without_changing_routing():
    readiness = assess_readiness(_s4(missing_evidence=True), "prd", [])

    assert readiness.ready_for_prd is False
    assert readiness.provisional is True
    assert any(finding.category == "evidence" for finding in readiness.findings)


def test_prd_readiness_discloses_unresolved_material_disagreement():
    readiness = assess_readiness(_s4(disagreement=True), "prd", [])

    assert readiness.ready_for_prd is False
    assert any(finding.category == "disagreement" for finding in readiness.findings)


def test_non_prd_route_is_not_a_hidden_readiness_gate():
    readiness = assess_readiness(
        _s4(),
        "poc",
        [Assumption(statement="Need validation", severity="Blocking", reason="Unproven")],
    )

    assert readiness.ready_for_prd is False
    assert readiness.provisional is True
    assert any(finding.category == "blocking_assumption" for finding in readiness.findings)


def test_prd_decision_memo_discloses_blocking_readiness_gaps():
    from app.models.stages import S5OutputData
    from app.stages.s5_prioritization import build_decision_memo

    readiness = assess_readiness(_s4(missing_evidence=True), "prd", [])
    memo = build_decision_memo(
        S5OutputData(
            impact_score=4,
            strategic_fit_score=4,
            feasibility_score=4,
            confidence_score=4,
            composite_score=4,
            routing="prd",
            assumptions=[],
            rationale="Candidate route.",
            blocking_count=0,
            readiness=readiness,
        )
    )

    assert "## Decision Readiness" in memo
    assert "Provisional" in memo
    assert "Missing pinned evidence" in memo
