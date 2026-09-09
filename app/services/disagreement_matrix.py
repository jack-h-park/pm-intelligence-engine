"""Deterministic evidence_v1 comparison of independently generated personas."""

from app.models.stages import DisagreementMatrix, OptionDisagreement, PersonaOutput


def build_disagreement_matrix(personas: list[PersonaOutput]) -> DisagreementMatrix:
    """Summarize only explicit option positions without inventing missing judgments."""
    by_option: dict[str, dict[str, PersonaOutput]] = {}
    for persona in personas:
        for option in persona.option_positions:
            by_option.setdefault(option, {})[persona.persona] = persona

    rows: list[OptionDisagreement] = []
    disagreements: list[str] = []
    for option in sorted(by_option):
        assessments = by_option[option]
        positions = {
            persona: assessments[persona].option_positions[option]
            for persona in sorted(assessments)
        }
        evidence_passage_ids = {
            persona: sorted(set(assessments[persona].evidence_passage_ids))
            for persona in sorted(assessments)
        }
        if len(positions) < 2:
            status = "insufficient_assessment"
        elif len(set(positions.values())) == 1:
            status = "consensus"
        else:
            status = "disagreement"
            disagreements.append(option)
        rows.append(
            OptionDisagreement(
                option=option,
                positions=positions,
                evidence_passage_ids=evidence_passage_ids,
                status=status,
            )
        )

    return DisagreementMatrix(
        options=rows,
        material_disagreement_options=disagreements,
    )
