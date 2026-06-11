"""Stage 2 — Insight Extraction.

LLM call: extracts "what changed", reframing, strategy pillar references,
and why the signal matters for the specific product.
"""

from app.logging import emit_event
from app.llm.json_call import complete_json
from app.llm.protocol import LLMProvider
from app.models.stages import RunContext, S2Input, S2Output, S2OutputData, StageMetadata
from app.services.template_service import TemplateService
from app.storage.protocol import PMWorkflowStore

_JSON_SCHEMA = """{
  "what_changed": "<concrete external change — what is different now vs before>",
  "reframing": "<common market framing> vs <correct framing for this product's segment>",
  "pillar_references": ["<pillar name or number from context>", "..."],
  "relevance_explanation": "<why this matters for this specific product — name a pillar, user segment, or pain point>",
  "relevance_score": <integer 1–5>,
  "suggested_mode": "<one of: file | brief | opportunity | evaluate | decide>",
  "suggestion_reasoning": "<one sentence explaining why this depth is appropriate>"
}"""

_MODE_GUIDANCE = """
## Relevance Scoring (1–5)

Score the strategic relevance of this signal for this specific product:

| Score | Meaning |
|-------|---------|
| 1 | Completely irrelevant — wrong product, wrong segment, no actionable implication |
| 2 | Marginally related — tangential mention only, no clear action possible |
| 3 | Borderline — potentially relevant but implication is unclear; PM should judge |
| 4 | Clearly relevant — connects to a named strategy pillar or specific user need |
| 5 | Highly relevant — direct, urgent, actionable implication for the product |

Signals scored 1–2 must use `suggested_mode: file`.
Signals scored 3–5 warrant PM attention at minimum.

## Suggested Pipeline Depth

After scoring relevance, recommend how deeply to process this signal:

| Mode | When to suggest |
|------|----------------|
| file | Signal is noise — wrong product, wrong segment, or purely informational with no action possible |
| brief | Signal is interesting but low urgency — worth noting the insight but no opportunity to pursue now |
| opportunity | Signal warrants framing as an opportunity but the team should decide before investing in full evaluation |
| evaluate | Signal is clearly relevant and an opportunity exists — run full 4-persona evaluation before deciding |
| decide | Signal is directly actionable, opportunity is obvious, and the team is ready to commit to a path |

Choose the minimum depth needed given the signal's relevance, urgency, and actionability."""


async def run(
    input: S2Input,
    context: RunContext,
    llm: LLMProvider,
    store: PMWorkflowStore,
) -> S2Output:
    from config import settings

    template_service = TemplateService(settings.DECISION_SYSTEM_ROOT)
    template = template_service.load_template("s2")

    system_message = (
        "You are a Product Manager. Follow the PM identity and operating philosophy below.\n\n"
        f"{context.pm_identity}"
    )

    user_message = f"""## Stage 2 Framework
{template}

---

## Product Context
{context.product_context}

---

## Stage 1 Signal Output
Title: {input.s1_output.title}
Category: {input.s1_output.category}
Summary:
{input.s1_output.summary}

---

{_MODE_GUIDANCE}

---

## Your Task
Apply the Stage 2 framework to the signal above.
Respond with a single JSON object matching this schema exactly — no markdown, no commentary:

{_JSON_SCHEMA}

Rules:
- "pillar_references" must contain at least one pillar name drawn from the Strategy Pillars section of the product context.
- "what_changed" must describe a concrete, specific external change — not a trend or feeling.
- "suggested_mode" must be exactly one of: file, brief, opportunity, evaluate, decide.
- Do not hallucinate facts not present in the signal or product context."""

    data = await complete_json(
        llm,
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ],
        stage="s2",
        run_id=context.run_id,
        max_tokens=1024,
        temperature=0,  # deterministic — relevance scoring must be reproducible run-to-run
    )
    output_data = S2OutputData(**data)

    output = S2Output(
        run_id=context.run_id,
        output=output_data,
        metadata=StageMetadata(model_used=_resolve_model()),
    )

    store.save_stage_output(
        run_id=context.run_id,
        stage="s2",
        output_json=output.model_dump_json(),
    )

    store.save_artifact(
        run_id=context.run_id,
        artifact_type="insight_memo",
        content_md=_build_insight_memo(input.s1_output.title, input.s1_output.category, output_data),
        content_json=output.model_dump_json(),
        source_stage="s2",
    )

    store.save_artifact(
        run_id=context.run_id,
        artifact_type="checkpoint",
        content_md=_build_checkpoint(input.s1_output.title, input.s1_output.category, output_data),
        content_json=output.model_dump_json(),
        source_stage="s2",
    )

    emit_event("s2", "completed", context.run_id, {"signal_id": input.signal_id})
    return output


def _build_checkpoint(title: str, category: str, data: S2OutputData) -> str:
    pillars = ", ".join(data.pillar_references) if data.pillar_references else "—"
    mode_next = {
        "file": "Pipeline complete — signal filed for reference.",
        "brief": "Pipeline complete at brief depth.",
        "opportunity": "Next: S3 Opportunity Framing",
        "evaluate": "Next: S3 → S4 Evaluation",
        "decide": "Next: S3 → S4 → S5 → S6 → S7",
    }.get(data.suggested_mode, f"Next: continue pipeline ({data.suggested_mode})")

    return f"""# Pipeline Checkpoint — S2 Complete

**Signal:** {title} ({category})
**Relevance:** {data.relevance_score}/5 · Suggested depth: `{data.suggested_mode}`

## What Changed
{data.what_changed}

## Why It Matters
{data.relevance_explanation}

## Reframing
{data.reframing}

## Strategy Pillars
{pillars}

---
**{mode_next}**
"""


def _build_insight_memo(title: str, category: str, data: S2OutputData) -> str:
    pillars = ", ".join(data.pillar_references) if data.pillar_references else "—"
    return f"""# Insight Memo

**Signal:** {title}
**Category:** {category}
**Relevance Score:** {data.relevance_score}/5
**Suggested Mode:** {data.suggested_mode}

## What Changed
{data.what_changed}

## Why It Matters
{data.relevance_explanation}

## Reframing
{data.reframing}

## Strategy Pillars
{pillars}
"""


def _resolve_model() -> str:
    from config import settings

    if settings.LLM_PROVIDER.lower() == "claude":
        return settings.ANTHROPIC_MODEL
    return settings.OPENAI_MODEL
