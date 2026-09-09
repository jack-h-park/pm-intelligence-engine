import json

import pytest

from app.agents.explorer import ExplorerAgent
from app.models.stages import RunContext, S3OutputData
from app.models.stages import PersonaOutput


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
