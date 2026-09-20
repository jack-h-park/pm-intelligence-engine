"""Unit tests for Stage 4 — Persona Evaluation and S4 rubric."""

import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.stages import (
    PersonaOutput,
    RunContext,
    S3OutputData,
    S4Input,
)
from eval.rubrics.s4_rubric import check as check_rubric


def _make_context(product_context: str = "", pipeline_version: str = "legacy") -> RunContext:
    return RunContext(
        run_id="test-run-s4",
        product_id="example-security-product",
        pm_identity="PM identity text",
        company_context="Company context",
        product_context=product_context
        or "Strategy Pillar: **Attack Surface Reduction**: Minimize exposed attack vectors.",
        decision_pipeline_version=pipeline_version,  # type: ignore[arg-type]
    )


def _make_s3_output() -> S3OutputData:
    return S3OutputData(
        problem_statement="No admin enforcement for the policy.",
        target_user="IT admin at government agency.",
        hypothesis="If the platform enforces the policy, then admins can mandate posture.",
        assumed_value_user="Admin-enforced security posture.",
        assumed_value_business="The platform ships enforcement before the OS's native equivalent.",
    )


def _make_store() -> MagicMock:
    store = MagicMock()
    store.save_stage_output = MagicMock(return_value="output-id")
    return store


@contextmanager
def _mock_persona_templates():
    """Stub TemplateService so S4 stage tests stay hermetic (no disk reads)."""
    with patch("app.stages.s4_evaluation.TemplateService") as MockTS:
        MockTS.return_value.load_persona_prompt.side_effect = lambda persona: {
            "lens": f"{persona} lens text",
            "question": f"{persona} evaluation question",
        }
        yield MockTS


def _make_persona(
    persona: str,
    score: int,
    key_argument: str = "",
    open_question: str = "",
    dimension: str = "Impact",
) -> PersonaOutput:
    return PersonaOutput(
        persona=persona,  # type: ignore[arg-type]
        dimension=dimension,
        score=score,
        key_argument=key_argument or f"{persona} argument referencing attack surface reduction.",
        open_question=open_question or f"What would a customer interview reveal about {persona}?",
    )


# ---------------------------------------------------------------------------
# S4 rubric unit tests
# ---------------------------------------------------------------------------


def test_rubric_perfect_score():
    product_context = "Strategy Pillar: **Attack Surface Reduction**: Minimize entry points."
    personas = [
        _make_persona(
            "explorer",
            4,
            "attack surface reduction is key here",
            "What would an interview reveal?",
            "Impact",
        ),
        _make_persona(
            "strategist",
            5,
            "attack surface reduction aligns with Pillar 1",
            "Can legal review confirm the strategy?",
            "Strategic Fit",
        ),
        _make_persona(
            "builder",
            4,
            "attack surface reduction feasible via the platform's existing APIs",
            "Needs an engineering spike to confirm.",
            "Feasibility",
        ),
        _make_persona(
            "skeptic",
            2,
            "attack surface reduction already covered by existing policies — this adds no new protection. Customers configuring the platform individually may not benefit at all.",  # noqa: E501
            "Would a customer interview reveal redundancy?",
            "Confidence",
        ),
    ]
    result = check_rubric(personas, product_context)
    assert result.total_score >= 9
    assert result.passed


def test_rubric_skeptic_missing():
    product_context = "Strategy Pillar: **Reduce Attack Surface**: Minimize entry points."
    personas = [
        _make_persona("explorer", 4, "attack surface pillar", "interview needed"),
        _make_persona("strategist", 5, "attack surface strategy", "legal review"),
        _make_persona("builder", 3, "attack surface feasibility", "engineering spike"),
        # No skeptic
        _make_persona("explorer", 4, "attack surface second view", "survey needed"),
    ]
    result = check_rubric(personas, product_context)
    assert any("skeptic" in issue.lower() for issue in result.issues)


def test_rubric_skeptic_data_gap_penalty():
    product_context = "Strategy Pillar: **Reduce Attack Surface**: Minimize entry points."
    personas = [
        _make_persona("explorer", 4, "attack surface reduction", "interview", "Impact"),
        _make_persona(
            "strategist", 5, "attack surface strategy pillar", "legal review", "Strategic Fit"
        ),
        _make_persona(
            "builder", 3, "attack surface feasibility", "engineering spike", "Feasibility"
        ),
        _make_persona(
            "skeptic", 1, "insufficient data to assess this opportunity", "survey", "Confidence"
        ),
    ]
    result = check_rubric(personas, product_context)
    assert result.skeptic_quality == 1
    assert any(
        "insufficient data" in issue.lower() or "data" in issue.lower() for issue in result.issues
    )


def test_rubric_all_same_scores_can_still_show_independent_lenses():
    product_context = "Strategy Pillar: **Reduce Attack Surface**: Minimize."
    personas = [
        _make_persona(
            "explorer", 3, "attack surface affects administrator impact", "interview", "Impact"
        ),
        _make_persona(
            "strategist", 3, "attack surface aligns with strategy", "legal review", "Strategic Fit"
        ),
        _make_persona(
            "builder", 3, "attack surface can be implemented", "engineering spike", "Feasibility"
        ),
        _make_persona("skeptic", 3, "attack surface identical score", "survey", "Confidence"),
    ]
    result = check_rubric(personas, product_context)
    assert result.persona_independence == 3
    assert not any("identical" in issue.lower() for issue in result.issues)


def test_evidence_rubric_flags_missing_evidence_and_uncertainty():
    product_context = "Strategy Pillar: **Reduce Attack Surface**: Minimize."
    personas = [
        _make_persona("explorer", 3, "attack surface", "customer interview"),
        _make_persona("strategist", 3, "attack surface strategy", "legal review"),
        _make_persona("builder", 3, "attack surface feasibility", "engineering spike"),
        _make_persona(
            "skeptic",
            3,
            "attack surface counterargument with enough detail to avoid the data gap penalty.",
            "customer survey",
        ),
    ]
    for persona in personas[:3]:
        persona.evidence_passage_ids = ["passage-1"]
        persona.uncertainties = ["A customer interview could change this judgment."]

    result = check_rubric(personas, product_context)

    assert result.evidence_linkage_quality == 1
    assert result.uncertainty_quality == 1
    assert any("evidence" in issue.lower() for issue in result.issues)
    assert any("uncertaint" in issue.lower() for issue in result.issues)


def test_rubric_no_actionable_questions_penalizes():
    product_context = "Strategy Pillar: **Reduce Attack Surface**: Minimize."
    personas = [
        _make_persona("explorer", 4, "attack surface", "How will this work?", "Impact"),
        _make_persona(
            "strategist", 5, "attack surface", "Is this the right direction?", "Strategic Fit"
        ),
        _make_persona("builder", 4, "attack surface", "Can we build this?", "Feasibility"),
        _make_persona(
            "skeptic",
            2,
            "This assumes customers need it but they already enforce each control individually. The unified posture adds zero security value for sophisticated customers.",  # noqa: E501
            "Will this matter?",
            "Confidence",
        ),
    ]
    result = check_rubric(personas, product_context)
    assert result.open_question_quality < 3


# ---------------------------------------------------------------------------
# S4 stage integration (mocked LLM)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s4_runs_4_agents_independently():
    """All 4 agents run and each produces a PersonaOutput."""
    from app.stages import s4_evaluation

    explorer_resp = json.dumps(
        {
            "score": 4,
            "key_argument": "Strong attack surface reduction opportunity referenced from Pillar 1.",
            "open_question": "What would a customer interview with IT admins reveal about their current configuration?",  # noqa: E501
        }
    )
    strategist_resp = json.dumps(
        {
            "score": 5,
            "key_argument": "Directly maps to attack surface Pillar 1 and the platform's competitive moat.",  # noqa: E501
            "open_question": "Can legal review confirm regulatory alignment?",
        }
    )
    builder_resp = json.dumps(
        {
            "score": 4,
            "key_argument": "attack surface composite policy feasible with existing platform APIs, no new OS hooks needed.",  # noqa: E501
            "open_question": "Engineering spike needed: does the OS expose the needed system flag?",
        }
    )
    skeptic_resp = json.dumps(
        {
            "score": 2,
            "key_argument": "Steelman counter: customers already configure each policy sub-control individually via the platform's existing tools. attack surface may already be covered. Admin enforcement adds only marketing value, not security value.",  # noqa: E501
            "open_question": "Would a customer interview reveal that IT admins consider per-control config sufficient?",  # noqa: E501
        }
    )

    call_count = 0
    responses = [explorer_resp, strategist_resp, builder_resp, skeptic_resp]

    async def mock_complete(messages, **kwargs):
        nonlocal call_count
        resp = responses[call_count % len(responses)]
        call_count += 1
        return resp

    llm = AsyncMock()
    llm.complete = mock_complete
    store = _make_store()
    context = _make_context()

    with _mock_persona_templates():
        output = await s4_evaluation.run(
            S4Input(s3_output=_make_s3_output()),
            context,
            llm,
            store,
        )

    assert len(output.output.personas) == 4
    personas_by_name = {p.persona: p for p in output.output.personas}
    assert "explorer" in personas_by_name
    assert "strategist" in personas_by_name
    assert "builder" in personas_by_name
    assert "skeptic" in personas_by_name

    # Skeptic score should differ from explorer
    assert personas_by_name["skeptic"].score != personas_by_name["explorer"].score
    assert call_count == 4


@pytest.mark.asyncio
async def test_s4_version_propagates():
    from app.stages import s4_evaluation

    resp = json.dumps(
        {
            "score": 3,
            "key_argument": "attack surface concern referenced in context.",
            "open_question": "Customer interview would clarify this.",
        }
    )
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=resp)
    store = _make_store()

    with _mock_persona_templates():
        output = await s4_evaluation.run(
            S4Input(s3_output=_make_s3_output(), version=2),
            _make_context(),
            llm,
            store,
        )
    assert output.version == 2


@pytest.mark.asyncio
async def test_evidence_v1_s4_attaches_a_deterministic_disagreement_matrix():
    from app.stages import s4_evaluation

    responses = [
        json.dumps(
            {
                "score": 3,
                "key_argument": "attack surface impact.",
                "open_question": "Customer interview?",
                "option_positions": {"Pilot": "support"},
            }
        ),
        json.dumps(
            {
                "score": 3,
                "key_argument": "attack surface fit.",
                "open_question": "Legal review?",
                "option_positions": {"Pilot": "support"},
            }
        ),
        json.dumps(
            {
                "score": 3,
                "key_argument": "attack surface feasibility.",
                "open_question": "Engineering spike?",
                "option_positions": {"Pilot": "oppose"},
            }
        ),
        json.dumps(
            {
                "score": 3,
                "key_argument": "attack surface concern.",
                "open_question": "Customer survey?",
                "option_positions": {"Pilot": "oppose"},
            }
        ),
    ]
    llm = AsyncMock()
    llm.complete = AsyncMock(side_effect=responses)

    with _mock_persona_templates():
        output = await s4_evaluation.run(
            S4Input(s3_output=_make_s3_output()),
            _make_context(pipeline_version="evidence_v1"),
            llm,
            _make_store(),
        )

    assert output.output.disagreement_matrix is not None
    assert output.output.disagreement_matrix.material_disagreement_options == ["Pilot"]


@pytest.mark.asyncio
async def test_s4_feedback_injected():
    """Feedback string appears in the message sent to the LLM."""
    from app.stages import s4_evaluation

    captured_messages: list = []

    async def mock_complete(messages, **kwargs):
        captured_messages.extend(messages)
        return json.dumps(
            {
                "score": 3,
                "key_argument": "attack surface revisited with feedback.",
                "open_question": "Customer interview needed.",
            }
        )

    llm = AsyncMock()
    llm.complete = mock_complete
    store = _make_store()

    feedback = "Please focus more on government segment specifically."
    with _mock_persona_templates():
        await s4_evaluation.run(
            S4Input(s3_output=_make_s3_output(), feedback=feedback),
            _make_context(),
            llm,
            store,
        )

    all_content = " ".join(m["content"] for m in captured_messages)
    assert feedback in all_content
