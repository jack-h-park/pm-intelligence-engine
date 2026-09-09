from app.models.stages import PersonaOutput
from app.services.disagreement_matrix import build_disagreement_matrix
from app.stages.s4_evaluation import build_evaluation_brief


def _persona(name: str, positions: dict[str, str], evidence: list[str]) -> PersonaOutput:
    return PersonaOutput(
        persona=name,  # type: ignore[arg-type]
        dimension=name,
        score=3,
        key_argument=f"{name} assessment.",
        open_question="What evidence would resolve this?",
        option_positions=positions,
        evidence_passage_ids=evidence,
    )


def test_disagreement_matrix_is_deterministic_and_does_not_invent_disagreement():
    personas = [
        _persona("skeptic", {"Keep status quo": "oppose"}, ["passage-2", "passage-1"]),
        _persona("explorer", {"Keep status quo": "oppose"}, ["passage-1"]),
        _persona("builder", {"Pilot policy": "support"}, ["passage-3"]),
        _persona("strategist", {"Pilot policy": "oppose"}, ["passage-4"]),
    ]

    matrix = build_disagreement_matrix(personas)

    assert [row.option for row in matrix.options] == ["Keep status quo", "Pilot policy"]
    assert matrix.options[0].status == "consensus"
    assert matrix.options[0].positions == {"explorer": "oppose", "skeptic": "oppose"}
    assert matrix.options[0].evidence_passage_ids == {
        "explorer": ["passage-1"],
        "skeptic": ["passage-1", "passage-2"],
    }
    assert matrix.options[1].status == "disagreement"
    assert matrix.material_disagreement_options == ["Pilot policy"]


def test_disagreement_matrix_leaves_single_assessment_unresolved():
    matrix = build_disagreement_matrix(
        [_persona("explorer", {"Pilot policy": "support"}, ["passage-1"])]
    )

    assert matrix.options[0].status == "insufficient_assessment"
    assert matrix.material_disagreement_options == []


def test_evaluation_brief_discloses_evidence_v1_disagreement_state():
    personas = [
        _persona("explorer", {"Pilot policy": "support"}, ["passage-1"]),
        _persona("skeptic", {"Pilot policy": "oppose"}, ["passage-2"]),
    ]

    from app.models.stages import S4RubricResult

    brief = build_evaluation_brief(
        personas,
        S4RubricResult(
            total_score=9,
            score_grounding=3,
            skeptic_quality=2,
            open_question_quality=2,
            persona_independence=2,
            passed=True,
        ),
        build_disagreement_matrix(personas),
    )

    assert "## Option Positions" in brief
    assert "Pilot policy" in brief
    assert "disagreement" in brief
