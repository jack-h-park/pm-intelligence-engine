"""Base class for all S4 persona agents."""

from __future__ import annotations

from typing import Optional

from app.llm.json_call import complete_json
from app.llm.protocol import LLMProvider
from app.models.stages import PersonaOutput, RunContext, S3OutputData
from app.services.decision_case import render_decision_case

_JSON_SCHEMA = """{
  "score": <integer 1-5>,
  "key_argument": "<2–4 sentence evaluation from your persona's lens>",
  "open_question": "<single most important open question — must name who answers it and how>"
}"""

_EVIDENCE_V1_JSON_SCHEMA = """{
  "score": <integer 1-5>,
  "key_argument": "<2–4 sentence evaluation from your persona's lens>",
  "open_question": "<single most important open question — must name who answers it and how>",
  "evidence_passage_ids": ["<case passage ID supporting or contradicting the judgment>"],
  "option_assessments": {"<case option>": "<persona-specific assessment>"},
  "uncertainties": ["<what would change this judgment>"]
}"""


class PersonaAgent:
    """Persona wiring only — identity, dimension, weight.

    The persona's lens and evaluation question are NOT defined here; they live
    in pm-decision-context (prompts/s4-personas/{persona}.md) and are passed in
    via ``prompt`` so workflow design stays owned by decision-context (US-37).
    """

    persona: str
    dimension: str
    weight: float

    async def evaluate(
        self,
        opportunity: S3OutputData,
        context: RunContext,
        llm: LLMProvider,
        prompt: dict,
        feedback: Optional[str] = None,
        usage_sink: Optional[list] = None,
    ) -> PersonaOutput:
        system = (
            f"You are the {self.persona.capitalize()} persona in a PM evaluation framework.\n\n"
            f"Your lens: {prompt['lens']}\n\n"
            f"PM Identity:\n{context.pm_identity}"
        )

        feedback_block = (
            f"\n\n## PM Revision Feedback\n{feedback}\nIncorporate this feedback in your re-evaluation."
            if feedback
            else ""
        )

        evidence_v1 = context.decision_pipeline_version == "evidence_v1"
        evidence_rules = """
- evidence_passage_ids may only name IDs from the pinned DecisionCase.
- Assess every supplied case option; status quo/defer is a valid conclusion.
- uncertainties must name what evidence would change the judgment.""" if evidence_v1 else ""
        schema = _EVIDENCE_V1_JSON_SCHEMA if evidence_v1 else _JSON_SCHEMA
        user = f"""## Product Context
{context.product_context}

## Opportunity to Evaluate
Problem: {opportunity.problem_statement}
Target user: {opportunity.target_user}
Hypothesis: {opportunity.hypothesis}
Value for user: {opportunity.assumed_value_user}
Value for business: {opportunity.assumed_value_business}

## Pinned Decision Case
{render_decision_case(context.decision_case)}

## Your Evaluation Question
{prompt['question']}
{feedback_block}

## Instructions
Answer from your persona's lens only. Do NOT consider other personas.
Scoring dimension you own: **{self.dimension}** (score 1–5).

Respond with a single JSON object — no markdown, no commentary:
{schema}

Rules:
- Score must reflect your dimension ({self.dimension}), grounded in specific product context above.
- key_argument must reference at least one concrete element from the product context (pillar, constraint, pain point, or competitive dynamic).
- open_question must name *who* can answer it and *how* (e.g., "customer interview", "engineering spike", "legal review").
- Do NOT default to "insufficient data" — steelman the strongest argument you can from available evidence.
- Do not recast a hypothesis as a confirmed fact or invent a customer need absent from the case.{evidence_rules}"""

        data = await complete_json(
            llm,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            stage="s4",
            run_id=context.run_id,
            usage_sink=usage_sink,
            max_tokens=512,
            temperature=0,  # deterministic — persona scores must be reproducible run-to-run
        )
        return PersonaOutput(
            persona=self.persona,  # type: ignore[arg-type]
            dimension=self.dimension,
            score=int(data["score"]),
            key_argument=data["key_argument"],
            open_question=data["open_question"],
            evidence_passage_ids=[str(item) for item in data.get("evidence_passage_ids", [])],
            option_assessments={
                str(option): str(assessment)
                for option, assessment in data.get("option_assessments", {}).items()
            },
            uncertainties=[str(item) for item in data.get("uncertainties", [])],
        )
