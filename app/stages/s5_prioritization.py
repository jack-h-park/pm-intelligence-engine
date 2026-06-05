"""Stage 5 — Prioritization and Routing.

Composite score is computed deterministically from S4 persona scores.
LLM classifies assumptions (Blocking / Informing) and writes a rationale.
Routing rule is deterministic code — never delegated to the LLM.
"""

import json

from app.logging import emit_event
from app.llm.protocol import LLMProvider
from app.models.stages import (
    Assumption,
    RunContext,
    S5Input,
    S5Output,
    S5OutputData,
    StageMetadata,
)
from app.services.template_service import TemplateService
from app.storage.protocol import PMWorkflowStore

_WEIGHTS = {
    "explorer": 0.35,    # Impact
    "strategist": 0.30,  # Strategic Fit
    "builder": 0.20,     # Feasibility
    "skeptic": 0.15,     # Confidence
}

_ASSUMPTION_JSON_SCHEMA = """{
  "assumptions": [
    {
      "statement": "<the assumption being tested>",
      "severity": "Blocking",
      "reason": "<why false = opportunity killed>"
    }
  ],
  "rationale": "<1–3 sentence rationale for the routing decision>"
}"""


async def run(
    stage_input: S5Input,
    context: RunContext,
    llm: LLMProvider,
    store: PMWorkflowStore,
) -> S5Output:
    """Compute composite score, classify assumptions, and route to prd/poc/kill."""
    from config import settings

    personas = stage_input.s4_output.personas
    scores = {p.persona: p.score for p in personas}

    # Deterministic composite score
    composite = round(
        sum(scores.get(persona, 3) * weight for persona, weight in _WEIGHTS.items()), 2
    )
    skeptic_score = scores.get("skeptic", 3)

    template_service = TemplateService(settings.DECISION_SYSTEM_ROOT)
    template = template_service.load_template("s5")

    system_message = (
        "You are a Product Manager. Follow the PM identity and operating philosophy below.\n\n"
        f"{context.pm_identity}"
    )

    persona_summary = "\n".join(
        f"- {p.persona.capitalize()} ({p.dimension}, score {p.score}/5): {p.key_argument}"
        for p in personas
    )
    open_questions = "\n".join(
        f"- [{p.persona.capitalize()}] {p.open_question}" for p in personas
    )

    user_message = f"""## Stage 5 Framework
{template}

---

## Product Context
{context.product_context}

---

## Stage 4 Persona Evaluation Results
Composite score (already computed): {composite}/5.00

Persona scores and arguments:
{persona_summary}

Open questions raised:
{open_questions}

---

## Your Task
1. Extract the assumptions underlying the low-confidence / high-risk signals in the persona arguments above.
2. For each assumption, classify it as Blocking (if false → opportunity killed) or Informing (if false → scope adjusted).
3. Write a 1–3 sentence rationale explaining the routing decision given the composite score ({composite}) and these assumptions.

Respond with a single JSON object matching this schema exactly — no markdown, no commentary:

{_ASSUMPTION_JSON_SCHEMA}

Rules:
- Only include assumptions that appear in the persona arguments or open questions above.
- A Blocking assumption requires BOTH conditions to be true:
    (1) The opportunity is worthless if the assumption is false, AND
    (2) No alternative path to the same value is described in the persona arguments.
  If any persona already mentions a workaround, fallback, or alternative implementation path,
  the assumption is Informing — not Blocking.
- An Informing assumption narrows scope, slows execution, or raises uncertainty,
  but an alternative path survives and the core value proposition holds.
- When in doubt, prefer Informing. Reserve Blocking for assumptions where failure
  leaves zero residual value with no alternative.
- Limit to 5 assumptions maximum."""

    raw = await llm.complete(
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ],
        max_tokens=1024,
        temperature=0,  # deterministic — blocking classification must be consistent
    )

    data = _parse_json(raw)
    assumptions = [Assumption(**a) for a in data.get("assumptions", [])]
    rationale = data.get("rationale", "")

    # Deterministic routing rule (never delegated to LLM)
    blocking = [a for a in assumptions if a.severity == "Blocking"]
    routing = _compute_routing(composite, blocking)

    output_data = S5OutputData(
        impact_score=scores.get("explorer", 3),
        strategic_fit_score=scores.get("strategist", 3),
        feasibility_score=scores.get("builder", 3),
        confidence_score=skeptic_score,
        composite_score=composite,
        routing=routing,
        assumptions=assumptions,
        rationale=rationale,
        blocking_count=len(blocking),
    )

    output = S5Output(
        run_id=context.run_id,
        output=output_data,
        metadata=StageMetadata(model_used=_resolve_model()),
    )

    store.save_stage_output(
        run_id=context.run_id,
        stage="s5",
        output_json=output.model_dump_json(),
    )

    store.save_artifact(
        run_id=context.run_id,
        artifact_type="decision_memo",
        content_md=_build_decision_memo(output_data),
        content_json=output.model_dump_json(),
        source_stage="s5",
    )

    emit_event(
        "s5",
        "completed",
        context.run_id,
        {
            "composite": composite,
            "routing": routing,
            "blocking_count": len(blocking),
        },
    )
    return output


def _build_decision_memo(data: S5OutputData) -> str:
    routing_label = data.routing.upper() if hasattr(data.routing, "upper") else str(data.routing).upper()

    blocking = [a for a in data.assumptions if a.severity == "Blocking"]
    informing = [a for a in data.assumptions if a.severity != "Blocking"]

    def _fmt_assumptions(items: list[Assumption]) -> str:
        if not items:
            return "None"
        return "\n".join(f"- {a.statement}" for a in items)

    return f"""# Decision Memo

**Routing:** {routing_label}
**Composite Score:** {data.composite_score}/5.00

## Score Breakdown
| Dimension | Score |
|-----------|-------|
| Impact (Explorer) | {data.impact_score}/5 |
| Strategic Fit (Strategist) | {data.strategic_fit_score}/5 |
| Feasibility (Builder) | {data.feasibility_score}/5 |
| Confidence (Skeptic) | {data.confidence_score}/5 |

## Blocking Assumptions
{_fmt_assumptions(blocking)}

## Informing Assumptions
{_fmt_assumptions(informing)}

## Rationale
{data.rationale}
"""


def _compute_routing(composite: float, blocking: list[Assumption]) -> str:
    """Deterministic routing — not delegated to the LLM."""
    if blocking or composite <= 1.5:
        return "kill"
    if composite >= 3.5:
        return "prd"
    return "poc"


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
