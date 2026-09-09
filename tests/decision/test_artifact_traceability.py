from app.models.decision_case import DecisionCase
from app.models.insights import PreparedFact
from app.models.stages import ArtifactTraceability, RequirementEvidenceLink
from app.services.artifact_traceability import build_artifact_traceability


def test_evidence_v1_artifact_traceability_preserves_case_and_unknown_baseline():
    traceability = ArtifactTraceability(
        decision_case_id="case-1",
        decision_case_revision=2,
        approved_option=None,
        provisional=True,
        requirement_links=[
            RequirementEvidenceLink(
                requirement="Show enforcement status to administrators.",
                evidence_passage_ids=["passage-device-1"],
                decision_rationale="Supports the selected investigation path.",
            )
        ],
        proposed_metrics=[
            "Proposed: measure policy adoption; baseline unknown; target requires validation."
        ],
    )

    assert traceability.approved_option is None
    assert traceability.provisional is True
    assert traceability.requirement_links[0].evidence_passage_ids == ["passage-device-1"]
    assert "baseline unknown" in traceability.proposed_metrics[0]


def test_traceability_rejects_evidence_outside_the_pinned_case():
    case = DecisionCase(
        prepared_context_id="prepared-1",
        prepared_context_revision=1,
        product_id="product",
        decision_question="Question?",
        input_origins=["direct"],
        confirmed_facts=[PreparedFact(text="Known", passage_ids=["passage-1"])],
    )

    try:
        build_artifact_traceability(
            case,
            None,
            [RequirementEvidenceLink(requirement="Requirement", evidence_passage_ids=["invented"], decision_rationale="No")],
            [],
        )
    except ValueError as exc:
        assert "outside the pinned decision case" in str(exc)
    else:
        raise AssertionError("expected invalid evidence reference to be rejected")


def test_prd_renders_traceability_as_provisional_evidence():
    from app.models.stages import PRDCompletenessCheck, S6BOutputData
    from app.stages.s6b_prd import build_prd

    traceability = ArtifactTraceability(
        decision_case_id="case-1", decision_case_revision=1, provisional=True,
        requirement_links=[RequirementEvidenceLink(requirement="Status", evidence_passage_ids=["p1"], decision_rationale="Reason")],
        proposed_metrics=["Proposed: adoption; baseline unknown; target requires validation."],
    )
    completeness = PRDCompletenessCheck(**{name: True for name in PRDCompletenessCheck.model_fields})
    prd = build_prd(S6BOutputData(
        problem_statement="Problem", target_user="User", success_metrics=[], user_stories=[], in_scope=[],
        out_of_scope=[], technical_dependencies=[], open_questions=[], risks=[], completeness=completeness,
        traceability=traceability,
    ))

    assert "## Decision Traceability" in prd
    assert "baseline unknown" in prd
