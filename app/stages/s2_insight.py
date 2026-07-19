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
  "what_changed": "<concrete external change — what is different now vs before. Facts from the signal ONLY; do not name the product or make product-specific claims here>",
  "reframing": "<common market framing> vs <correct framing for this product's segment>",
  "pillar_references": ["<pillar name or number from context>", "..."],
  "claims": [
    {"text": "<one claim, single provenance>", "source": "signal | product_context | inference", "grounds": [<1-based positions of the claims this is derived from; required & non-empty when source is inference, else []>]}
  ],
  "relevance_score": <integer 1–5>,
  "suggested_mode": "<one of: archive | note | structure | evaluate | decide>",
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

Signals scored 1–2 must use `suggested_mode: archive`.
Signals scored 3–5 warrant PM attention at minimum.

## Suggested Pipeline Depth

After scoring relevance, recommend how deeply to process this signal:

Modes are a depth ladder (shallow → deep). Pick the minimum depth needed.

| Mode | When to suggest |
|------|----------------|
| archive | Signal is noise — wrong product, wrong segment, or purely informational with no action possible (set aside, not pursued) |
| note | Signal is interesting but low urgency — worth recording the insight but no opportunity to pursue now |
| structure | Signal warrants structuring into an opportunity, but the team should decide before investing in full evaluation |
| evaluate | Signal is clearly relevant and an opportunity exists — run full 4-persona evaluation before deciding |
| decide | Signal is directly actionable, opportunity is obvious, and the team is ready to commit to a path |

Choose the minimum depth needed given the signal's relevance, urgency, and actionability.
When torn between two adjacent depths, suggest the SHALLOWER one: a shallow run
can always be deepened later at no re-work cost, whereas an over-deep suggestion
wastes PM review time. Suggest `structure` or deeper only when the signal names a
concrete, product-specific opportunity — "relevant and worth watching" is `note`.
Canonical definition: pm-decision-context/core/02-workflow.md ("Processing Depth — 5 modes")."""


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
- "what_changed" must describe a concrete, specific external change drawn ONLY from the signal — not a trend, a feeling, or a product-specific claim. Do not name the product here.
- "claims" explains why the signal matters, as a list of single-provenance claims. Each claim carries exactly one "source":
  - "signal": a fact stated in the Stage 1 Signal Output above.
  - "product_context": a fact drawn from the Product Context above.
  - "inference": a conclusion you derive — it MUST list in "grounds" the 1-based positions of the signal/product_context claims it rests on.
  - Include at least one "inference" claim. Never mix provenances within a single claim — split them into separate claims instead.
- "suggested_mode" must be exactly one of: archive, note, structure, evaluate, decide.
- Do not hallucinate facts not present in the signal or product context."""

    usage_sink: list = []
    data = await complete_json(
        llm,
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ],
        stage="s2",
        run_id=context.run_id,
        usage_sink=usage_sink,
        max_tokens=1024,
        temperature=0,  # deterministic — relevance scoring must be reproducible run-to-run
    )
    output_data = S2OutputData(**data)

    output = S2Output(
        run_id=context.run_id,
        output=output_data,
        metadata=StageMetadata.with_usage(_resolve_model(), usage_sink),
    )

    store.save_stage_output(
        run_id=context.run_id,
        stage="s2",
        output_json=output.model_dump_json(),
    )

    store.save_artifact(
        run_id=context.run_id,
        artifact_type="insight_memo",
        content_md=build_insight_memo(input.s1_output.title, input.s1_output.category, output_data),
        source_stage="s2",
    )


    emit_event("s2", "completed", context.run_id, {"signal_id": input.signal_id})
    return output


_SOURCE_TAG = {"signal": "signal", "product_context": "context", "inference": "inferred"}


def _render_claims(data: S2OutputData) -> str:
    """Render the provenance-tagged 'why it matters' claims.

    Each line is numbered and prefixed with its source so a reviewer can tell
    signal facts from product context from engine inference at a glance;
    inference lines show `← n, m` tracing back to the claims they rest on.
    """
    if not data.claims:
        return "—"
    lines = []
    for i, claim in enumerate(data.claims, start=1):
        tag = _SOURCE_TAG.get(claim.source, claim.source)
        if claim.source == "inference" and claim.grounds:
            tag = f"{tag} ← {', '.join(str(g) for g in claim.grounds)}"
        lines.append(f"{i}. [{tag}] {claim.text}")
    return "\n".join(lines)



def build_insight_memo(title: str, category: str, data: S2OutputData) -> str:
    pillars = ", ".join(data.pillar_references) if data.pillar_references else "—"
    return f"""# Insight Memo

**Signal:** {title}
**Category:** {category}
**Relevance Score:** {data.relevance_score}/5
**Suggested Mode:** {data.suggested_mode}

## What Changed
{data.what_changed}

## Why It Matters
{_render_claims(data)}

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
