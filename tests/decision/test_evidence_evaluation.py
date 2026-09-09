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
