"""Base class for all S4 persona agents."""

from __future__ import annotations

import json
from typing import Optional

from app.llm.protocol import LLMProvider
from app.models.stages import PersonaOutput, RunContext, S3OutputData

_JSON_SCHEMA = """{
  "score": <integer 1-5>,
  "key_argument": "<2–4 sentence evaluation from your persona's lens>",
  "open_question": "<single most important open question — must name who answers it and how>"
}"""


class PersonaAgent:
    persona: str
    dimension: str
    weight: float
    system_prompt: str
    question: str

    async def evaluate(
        self,
        opportunity: S3OutputData,
        context: RunContext,
        llm: LLMProvider,
        feedback: Optional[str] = None,
    ) -> PersonaOutput:
        system = (
            f"You are the {self.persona.capitalize()} persona in a PM evaluation framework.\n\n"
            f"Your lens: {self.system_prompt}\n\n"
            f"PM Identity:\n{context.pm_identity}"
        )

        feedback_block = (
            f"\n\n## PM Revision Feedback\n{feedback}\nIncorporate this feedback in your re-evaluation."
            if feedback
            else ""
        )

        user = f"""## Product Context
{context.product_context}

## Opportunity to Evaluate
Problem: {opportunity.problem_statement}
Target user: {opportunity.target_user}
Hypothesis: {opportunity.hypothesis}
Value for user: {opportunity.assumed_value_user}
Value for business: {opportunity.assumed_value_business}

## Your Evaluation Question
{self.question}
{feedback_block}

## Instructions
Answer from your persona's lens only. Do NOT consider other personas.
Scoring dimension you own: **{self.dimension}** (score 1–5).

Respond with a single JSON object — no markdown, no commentary:
{_JSON_SCHEMA}

Rules:
- Score must reflect your dimension ({self.dimension}), grounded in specific product context above.
- key_argument must reference at least one concrete element from the product context (pillar, constraint, pain point, or competitive dynamic).
- open_question must name *who* can answer it and *how* (e.g., "customer interview", "engineering spike", "legal review").
- Do NOT default to "insufficient data" — steelman the strongest argument you can from available evidence."""

        raw = await llm.complete(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=512,
        )

        data = _parse_json(raw)
        return PersonaOutput(
            persona=self.persona,  # type: ignore[arg-type]
            dimension=self.dimension,
            score=int(data["score"]),
            key_argument=data["key_argument"],
            open_question=data["open_question"],
        )


def _parse_json(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return json.loads(text)
