import pytest

from app.models.decision_case import DecisionCase, InsightRevisionReference
from app.models.insights import PreparedContext, PreparedFact
from app.models.stages import RunContext
from app.services.decision_case import build_decision_case


def _prepared_context() -> PreparedContext:
    return PreparedContext(
        prepared_context_id="prepared-android",
        revision=3,
        candidate_id="candidate-android",
        bundle_id="bundle-android",
        question="How should we respond to the observed sharing behavior?",
        facts=[
            PreparedFact(
                text="The managed profile allowed a selected share target.",
                passage_ids=["passage-1"],
            )
        ],
        hypotheses=["The behavior may weaken isolation expectations."],
        constraints=["Do not claim enforcement without device evidence."],
        validation_status="valid",
        context_revision="fixture-v1",
    )


def test_direct_case_preserves_confirmed_facts_and_hypotheses_separately():
    case = build_decision_case(
        prepared_context=_prepared_context(),
        product_id="android-enterprise",
        decision_question="Should we investigate a policy change?",
        authorized_depth="evaluate",
        input_origin="direct",
        options=["Investigate", "Keep the current policy"],
    )

    assert case.input_origin == "direct"
    assert case.confirmed_facts[0].passage_ids == ["passage-1"]
    assert case.hypotheses == ["The behavior may weaken isolation expectations."]
    assert case.options[-1] == "Keep the current policy"
    assert case.insight_references == []


def test_insight_backed_case_pins_the_selected_immutable_revision():
    case = build_decision_case(
        prepared_context=_prepared_context(),
        product_id="android-enterprise",
        decision_question="What is the lowest-risk next decision?",
        authorized_depth="decide",
        input_origin="insight",
        insight_references=[InsightRevisionReference(insight_id="insight-7", revision=2)],
    )

    assert case.prepared_context_id == "prepared-android"
    assert case.prepared_context_revision == 3
    assert case.insight_references == [InsightRevisionReference(insight_id="insight-7", revision=2)]


def test_case_rejects_unattributed_confirmed_facts():
    with pytest.raises(ValueError, match="passage"):
        DecisionCase(
            prepared_context_id="prepared-android",
            prepared_context_revision=1,
            product_id="android-enterprise",
            decision_question="Should we act?",
            input_origins=["direct"],
            confirmed_facts=[{"text": "An unsupported claim", "passage_ids": []}],
        )


def test_run_context_keeps_the_case_outside_the_legacy_signal_summary():
    case = build_decision_case(
        prepared_context=_prepared_context(),
        product_id="android-enterprise",
        decision_question="Should we investigate a policy change?",
        authorized_depth="evaluate",
        input_origin="direct",
    )

    context = RunContext(
        run_id="run-1",
        product_id="android-enterprise",
        pm_identity="identity",
        company_context="company",
        product_context="product",
        decision_case=case,
    )

    assert context.decision_case == case
