import json

import pytest

from app.agents.explorer import ExplorerAgent
from app.models.stages import PersonaOutput, RunContext, S3OutputData


def test_persona_output_keeps_evidence_options_and_uncertainty_separate():
    output = PersonaOutput(
        persona="skeptic",
        dimension="Confidence",
        score=2,
        key_argument="The selected option lacks direct device evidence.",
        open_question="Can device testing confirm enforcement?",
        evidence_passage_ids=["passage-device-1"],
        option_assessments={"Retain status quo": "Avoids an unsupported product commitment."},
        uncertainties=["No device validation was supplied."],
    )

    assert output.evidence_passage_ids == ["passage-device-1"]
    assert output.option_assessments["Retain status quo"].startswith("Avoids")
    assert output.uncertainties == ["No device validation was supplied."]


def test_legacy_persona_output_remains_readable_without_evidence_fields():
    output = PersonaOutput(
        persona="explorer",
        dimension="Impact",
        score=3,
        key_argument="The opportunity may affect administrators.",
        open_question="Would interviews validate the impact?",
    )

    assert output.evidence_passage_ids == []
    assert output.option_assessments == {}
    assert output.uncertainties == []


@pytest.mark.asyncio
async def test_only_evidence_v1_personas_receive_the_evidence_schema():
    class FixtureLLM:
        def __init__(self):
            self.messages = None

        async def complete(self, messages, **kwargs):
            self.messages = messages
            return json.dumps(
                {
                    "score": 3,
                    "key_argument": "A bounded assessment.",
                    "open_question": "Can a customer interview resolve this?",
                }
            )

    llm = FixtureLLM()
    context = RunContext(
        run_id="run-evidence",
        product_id="product",
        pm_identity="identity",
        company_context="company",
        product_context="product context",
        decision_pipeline_version="evidence_v1",
    )
    await ExplorerAgent().evaluate(
        S3OutputData(
            problem_statement="Problem",
            target_user="User",
            hypothesis="If action, outcome.",
            assumed_value_user="Value",
            assumed_value_business="Business value",
        ),
        context,
        llm,
        prompt={"lens": "lens", "question": "question"},
    )

    assert "evidence_passage_ids" in llm.messages[1]["content"]


def _fixture_llm(option_positions: dict[str, str]):
    class FixtureLLM:
        async def complete(self, messages, **kwargs):
            return json.dumps(
                {
                    "score": 3,
                    "key_argument": "A bounded assessment.",
                    "open_question": "Can a customer interview resolve this?",
                    "option_assessments": {
                        option: f"Assessment of {option}" for option in option_positions
                    },
                    "option_positions": option_positions,
                }
            )

    return FixtureLLM()


async def _evaluate_with_positions(option_positions: dict[str, str]) -> PersonaOutput:
    context = RunContext(
        run_id="run-positions",
        product_id="product",
        pm_identity="identity",
        company_context="company",
        product_context="product context",
        decision_pipeline_version="evidence_v1",
    )
    return await ExplorerAgent().evaluate(
        S3OutputData(
            problem_statement="Problem",
            target_user="User",
            hypothesis="If action, outcome.",
            assumed_value_user="Value",
            assumed_value_business="Business value",
        ),
        context,
        _fixture_llm(option_positions),
        prompt={"lens": "lens", "question": "question"},
    )


@pytest.mark.asyncio
async def test_option_positions_are_normalised_before_they_reach_the_model():
    """Casing and stray whitespace are the model's, not a different judgment."""
    output = await _evaluate_with_positions({"Pilot": "Support", "Defer": " OPPOSE "})

    assert output.option_positions == {"Pilot": "support", "Defer": "oppose"}


@pytest.mark.asyncio
async def test_an_unrecognised_position_is_dropped_rather_than_failing_the_persona():
    """A hedged position is not mapped onto a neighbour, and does not end the stage.

    Before this coercion, the unrecognised value raised a pydantic
    ValidationError on PersonaOutput; S4 gathers the four personas without
    return_exceptions, so one such value ended the whole evaluation stage.
    """
    output = await _evaluate_with_positions(
        {"Pilot": "support", "Defer": "lean oppose", "Retain": "uncertain"}
    )

    assert output.option_positions == {"Pilot": "support", "Retain": "uncertain"}
    # The model's own wording is still readable in the free-text assessment.
    assert output.option_assessments["Defer"] == "Assessment of Defer"
