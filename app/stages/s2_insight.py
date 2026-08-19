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
  "depth_basis": "<one of: no_product_surface | trend_only | named_gap | options_exist | commit_ready>",
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

Relevance does **not** decide the depth. Score it here, then classify the depth
basis below on its own evidence — the two questions have different answers often
enough that treating the second as a consequence of the first is the main way this
stage goes wrong.

## Depth basis — decide this BEFORE the depth, and on different grounds

Relevance answers *how much this matters to the product*. Depth answers *what there
is to work on*, which is a different question, and answering it in relevance's
vocabulary makes the second answer a restatement of the first.

So first classify what the signal actually **contains**. Judge only what is on the
page — not how important the topic feels, not how relevant you scored it:

| depth_basis | The signal… |
|---|---|
| no_product_surface | does not touch anything this product owns or would have to answer for |
| trend_only | is about this product's space, but names no specific mechanism, incident, or capability gap — it describes a direction, not a thing |
| named_gap | names a specific mechanism, incident, CVE or capability that this product would have to answer |
| options_exist | names that gap **and** there is more than one distinguishable way the product could respond |
| commit_ready | the response is already clear, and the open question is only whether to do it |

A signal can be highly relevant and still be `trend_only` — an important article
about a direction the product cares about, naming nothing it must answer, is
`trend_only`. That combination is common and is not a contradiction.

## Suggested depth follows from the basis

| depth_basis | suggested_mode |
|---|---|
| no_product_surface | archive |
| trend_only | note |
| named_gap | structure |
| options_exist | evaluate |
| commit_ready | decide |

Two constraints override the table:

1. A signal scored 1–2 on relevance is `archive` regardless of basis — nobody should
   spend a decision on it.
2. When torn between two adjacent bases, choose the SHALLOWER. A shallow run can be
   deepened later at no re-work cost; an over-deep suggestion spends PM review time
   that cannot be refunded.

State the basis in `suggestion_reasoning` by naming the specific thing you found —
or by saying that you found none. "Clearly relevant, but no concrete product
implication yet" is a complete and correct reason for `note` on a 4.
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
- "depth_basis" must be exactly one of: no_product_surface, trend_only, named_gap, options_exist, commit_ready — and must describe what the signal CONTAINS, not how relevant it is.
- "suggested_mode" must be exactly one of: archive, note, structure, evaluate, decide, and must follow from "depth_basis" per the table above.
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
    # Runs stored before `depth_basis` existed do not carry it, and this renderer
    # is used to export them. Omit the line rather than printing a default that
    # would read as a judgement the run never made.
    basis = getattr(data, "depth_basis", None)
    basis_line = f"\n**Depth Basis:** {basis}" if basis else ""
    return f"""# Insight Memo

**Signal:** {title}
**Category:** {category}
**Relevance Score:** {data.relevance_score}/5
**Suggested Mode:** {data.suggested_mode}{basis_line}

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
