"""Stage 6A — PoC Planning.

Converts Blocking assumptions from S5 into a minimum experiment design.
Only runs when S5 routing is 'poc'.
"""

import json

from app.logging import emit_event
from app.llm.protocol import LLMProvider
from app.models.stages import (
    RunContext,
    S6AInput,
    S6AOutput,
    S6AOutputData,
    StageMetadata,
)
from app.services.template_service import TemplateService
from app.storage.protocol import PMWorkflowStore

_JSON_SCHEMA = """{
  "experiment_goal": "<one sentence: what assumption are we validating and why it matters>",
  "blocking_assumptions_addressed": [
    "<assumption 1 being tested>",
    "<assumption 2 being tested>"
  ],
  "experiment_design": "<multi-sentence description: specific actions, owners, and minimum test>",
  "success_criteria": "<what result confirms the blocking assumptions hold — must be binary>",
  "timeline_weeks": <integer>,
  "resources_needed": "<roles and rough effort, no engineering before assumptions validated>"
}"""


async def run(
    stage_input: S6AInput,
    context: RunContext,
    llm: LLMProvider,
    store: PMWorkflowStore,
) -> S6AOutput:
    """Generate a minimum experiment design targeting the Blocking assumptions from S5."""
    from config import settings

    template_service = TemplateService(settings.DECISION_SYSTEM_ROOT)
    template = template_service.load_template("s6a")

    s5 = stage_input.s5_output
    blocking = [a for a in s5.assumptions if a.severity == "Blocking"]
    informing = [a for a in s5.assumptions if a.severity == "Informing"]

    blocking_text = "\n".join(f"- {a.statement} (Blocking: {a.reason})" for a in blocking)
    informing_text = "\n".join(f"- {a.statement}" for a in informing) if informing else "None"

    system_message = (
        "You are a Product Manager. Follow the PM identity and operating philosophy below.\n\n"
        f"{context.pm_identity}"
    )

    user_message = f"""## Stage 6A Framework
{template}

---

## Product Context
{context.product_context}

---

## Stage 5 Prioritization Output
Composite score: {s5.composite_score}/5.00
Routing: {s5.routing}
Rationale: {s5.rationale}

Blocking assumptions (must be addressed):
{blocking_text}

Informing assumptions (nice to validate):
{informing_text}

---

## Your Task
Design the minimum viable experiment that validates the Blocking assumptions above.
The experiment must NOT require engineering resources before assumptions are validated.

Respond with a single JSON object matching this schema exactly — no markdown, no commentary:

{_JSON_SCHEMA}

Rules:
- experiment_design must address every Blocking assumption listed above.
- Resources should be the cheapest possible test (customer interviews, competitor research, design mockup, etc.).
- timeline_weeks should be realistic given the resources described — typically 2–6 weeks.
- success_criteria must be binary (pass/fail) — not "learn more about"."""

    raw = await llm.complete(
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ],
        max_tokens=1024,
    )

    data = _parse_json(raw)
    output_data = S6AOutputData(**data)

    output = S6AOutput(
        run_id=context.run_id,
        output=output_data,
        metadata=StageMetadata(model_used=_resolve_model()),
    )

    store.save_stage_output(
        run_id=context.run_id,
        stage="s6a",
        output_json=output.model_dump_json(),
    )

    emit_event("s6a", "completed", context.run_id, {"timeline_weeks": output_data.timeline_weeks})
    return output


def _parse_json(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return json.loads(text)


def _resolve_model() -> str:
    from config import settings

    if settings.LLM_PROVIDER.lower() == "claude":
        return settings.ANTHROPIC_MODEL
    return settings.OPENAI_MODEL
