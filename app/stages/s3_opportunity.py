"""Stage 3 — Opportunity Creation.

LLM call: converts the S2 insight into a structured, evaluatable opportunity
with a falsifiable hypothesis.
"""

from app.logging import emit_event
from app.llm.json_call import complete_json
from app.llm.protocol import LLMProvider
from app.models.stages import RunContext, S3Input, S3Output, S3OutputData, StageMetadata
from app.services.template_service import TemplateService
from app.storage.protocol import PMWorkflowStore

_JSON_SCHEMA = """{
  "problem_statement": "<specific problem for the target user, grounded in the insight>",
  "target_user": "<role + context — more specific than the broad user segment in context.md>",
  "hypothesis": "If we <action>, then <outcome> will happen, because <reason>.",
  "assumed_value_user": "<specific, concrete benefit for the user>",
  "assumed_value_business": "<specific, concrete benefit for the business>",
  "value_horizon": "<durable | transient>"
}"""


async def run(
    input: S3Input,
    context: RunContext,
    llm: LLMProvider,
    store: PMWorkflowStore,
) -> S3Output:
    from config import settings

    template_service = TemplateService(settings.DECISION_SYSTEM_ROOT)
    template = template_service.load_template("s3")

    system_message = (
        "You are a Product Manager. Follow the PM identity and operating philosophy below.\n\n"
        f"{context.pm_identity}"
    )

    user_message = f"""## Stage 3 Framework
{template}

---

## Product Context
{context.product_context}

---

## Stage 2 Insight Output
What changed: {input.s2_output.what_changed}

Reframing: {input.s2_output.reframing}

Strategy pillars referenced: {", ".join(input.s2_output.pillar_references)}

Why it matters: {input.s2_output.relevance_explanation}

---

## Your Task
Apply the Stage 3 framework to convert the insight above into a structured opportunity.
Respond with a single JSON object matching this schema exactly — no markdown, no commentary:

{_JSON_SCHEMA}

Rules:
- "hypothesis" must be falsifiable — it must be possible to design an experiment that proves it wrong.
- "problem_statement" describes a problem, not a solution or feature.
- "target_user" must be more specific than the segment defined in context.md.
- Both value fields must be distinct and concrete — not generic statements.
- "value_horizon": is this opportunity's value **durable** (compounds / defensible
  over time) or **transient** (a closing window — a platform vendor or competitor
  may erase the value, e.g. by shipping a native capability)? Choose "transient"
  only when there is a concrete reason the window may close; otherwise "durable"."""

    usage_sink: list = []
    data = await complete_json(
        llm,
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ],
        stage="s3",
        run_id=context.run_id,
        usage_sink=usage_sink,
        max_tokens=1024,
        temperature=0,  # deterministic — opportunity framing must be reproducible run-to-run
    )
    output_data = S3OutputData(**data)

    output = S3Output(
        run_id=context.run_id,
        output=output_data,
        metadata=StageMetadata.with_usage(_resolve_model(), usage_sink),
    )

    store.save_stage_output(
        run_id=context.run_id,
        stage="s3",
        output_json=output.model_dump_json(),
    )

    store.save_artifact(
        run_id=context.run_id,
        artifact_type="opportunity_memo",
        content_md=_build_opportunity_memo(output_data),
        content_json=output.model_dump_json(),
        source_stage="s3",
    )


    emit_event("s3", "completed", context.run_id, {"signal_id": input.signal_id})
    return output



def _build_opportunity_memo(data: S3OutputData) -> str:
    from app.models.stages import render_value_horizon

    return f"""# Opportunity Memo

## Problem Statement
{data.problem_statement}

## Target User
{data.target_user}

## Hypothesis
{data.hypothesis}

## Value
- **User:** {data.assumed_value_user}
- **Business:** {data.assumed_value_business}

## Value Horizon
{render_value_horizon(data.value_horizon)}
"""


def _resolve_model() -> str:
    from config import settings

    if settings.LLM_PROVIDER.lower() == "claude":
        return settings.ANTHROPIC_MODEL
    return settings.OPENAI_MODEL
