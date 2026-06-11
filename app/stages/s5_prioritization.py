"""Stage 5 — Prioritization and Routing.

Composite score is computed deterministically from S4 persona scores.
LLM classifies assumptions (Blocking / Adjusting) and writes a rationale.
Routing rule is deterministic code — never delegated to the LLM.
"""

from app.logging import emit_event
from app.llm.json_call import complete_json
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

_DEFAULT_WEIGHTS = {
    "explorer": 0.35,    # Impact
    "strategist": 0.30,  # Strategic Fit
    "builder": 0.20,     # Feasibility
    "skeptic": 0.15,     # Confidence
}

_WEIGHT_KEYS = {"impact": "explorer", "strategic_fit": "strategist", "feasibility": "builder", "confidence": "skeptic"}

_DEFAULT_THRESHOLDS = {
    "kill_threshold": 1.5,    # composite at or below -> kill
    "prd_threshold": 3.5,     # composite at or above -> PRD-eligible
    "confidence_gate": 4,     # confidence at or above -> prd, below -> poc
}


def _load_weights(product_id: str) -> dict:
    """Load per-product scoring weights from pm-decision-context.

    Reads {DECISION_CONTEXT_ROOT}/products/{product_id}/scoring.yaml.
    Falls back to _DEFAULT_WEIGHTS if the file is absent or malformed.

    scoring.yaml format:
      impact: 0.35
      strategic_fit: 0.30
      feasibility: 0.20
      confidence: 0.15
    """
    import pathlib
    import yaml
    from config import settings

    path = pathlib.Path(settings.DECISION_CONTEXT_ROOT) / "products" / product_id / "scoring.yaml"
    if not path.exists():
        return _DEFAULT_WEIGHTS.copy()

    try:
        raw = yaml.safe_load(path.read_text())
        if not isinstance(raw, dict):
            return _DEFAULT_WEIGHTS.copy()
        weights = {}
        for yaml_key, persona_key in _WEIGHT_KEYS.items():
            val = raw.get(yaml_key)
            if isinstance(val, (int, float)) and val > 0:
                weights[persona_key] = float(val)
        if len(weights) != 4:
            return _DEFAULT_WEIGHTS.copy()
        # Normalise so weights always sum to 1.0
        total = sum(weights.values())
        return {k: round(v / total, 6) for k, v in weights.items()}
    except Exception:  # noqa: BLE001
        return _DEFAULT_WEIGHTS.copy()


def _load_thresholds(product_id: str) -> dict:
    """Load per-product routing thresholds from pm-decision-context.

    Reads the same {DECISION_CONTEXT_ROOT}/products/{product_id}/scoring.yaml
    as _load_weights. Falls back to _DEFAULT_THRESHOLDS for any key that is
    absent or malformed.

    scoring.yaml keys (all optional):
      kill_threshold: 1.5
      prd_threshold: 3.5
      confidence_gate: 4
    """
    import pathlib
    import yaml
    from config import settings

    path = pathlib.Path(settings.DECISION_CONTEXT_ROOT) / "products" / product_id / "scoring.yaml"
    if not path.exists():
        return _DEFAULT_THRESHOLDS.copy()

    try:
        raw = yaml.safe_load(path.read_text())
        if not isinstance(raw, dict):
            return _DEFAULT_THRESHOLDS.copy()
        thresholds = _DEFAULT_THRESHOLDS.copy()
        for key in _DEFAULT_THRESHOLDS:
            val = raw.get(key)
            if isinstance(val, (int, float)) and val > 0:
                thresholds[key] = float(val)
        return thresholds
    except Exception:  # noqa: BLE001
        return _DEFAULT_THRESHOLDS.copy()


_ASSUMPTION_JSON_SCHEMA = """{
  "assumptions": [
    {
      "statement": "<the assumption being tested>",
      "severity": "Blocking",
      "reason": "<why false = opportunity killed>"
    }
  ],
  "rationale": "<1–3 sentence rationale for the routing decision>",
  "governing_heuristics": ["<decision heuristic number(s) from the PM identity that governed this call, e.g. #7, #14>"]
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

    weights = _load_weights(context.product_id)
    # Deterministic composite score
    composite = round(
        sum(scores.get(persona, 3) * weight for persona, weight in weights.items()), 2
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
2. For each assumption, classify it as Blocking (if false → nothing worth building is left → opportunity killed) or Adjusting (if false → a smaller or different version still survives).
3. Write a 1–3 sentence rationale explaining the routing decision given the composite score ({composite}) and these assumptions.
4. Cite the decision heuristic number(s) from the PM identity above (e.g. #7, #14) that most directly governed this call.

Respond with a single JSON object matching this schema exactly — no markdown, no commentary:

{_ASSUMPTION_JSON_SCHEMA}

Rules:
- Blocking gates WHETHER you build; Adjusting gates HOW you build. Two-question test:
  Q1 (gate) "if this assumption is false, is there STILL a version worth building?"
  No → Blocking. Yes → it is Adjusting (Q2: the surviving version is just smaller/different/slower).
- Only include assumptions that appear in the persona arguments or open questions above.
- A Blocking assumption requires BOTH conditions to be true:
    (1) The opportunity is worthless if the assumption is false, AND
    (2) No alternative path to the same value is described in the persona arguments.
  If any persona already mentions a workaround, fallback, or alternative implementation path,
  the assumption is Adjusting — not Blocking.
- An Adjusting assumption narrows scope, slows execution, or raises uncertainty,
  but an alternative path survives and the core value proposition holds.
- "A competitor or platform vendor might close this gap later" is Adjusting
  (timing/scope), NOT Blocking, unless closure is imminent AND leaves no residual value now.
- When in doubt, prefer Adjusting. Reserve Blocking for assumptions where failure
  leaves zero residual value with no alternative. See the worked examples (R06 Kill
  vs R04 PRD) in the Stage 5 Framework above.
- governing_heuristics: cite the 1–3 heuristic numbers that actually drove the
  decision (e.g. ["#7", "#14"]); use [] if none clearly apply.
- Limit to 5 assumptions maximum."""

    data = await complete_json(
        llm,
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ],
        stage="s5",
        run_id=context.run_id,
        max_tokens=1024,
        temperature=0,  # deterministic — blocking classification must be consistent
    )
    assumptions = [Assumption(**a) for a in data.get("assumptions", [])]
    rationale = data.get("rationale", "")
    governing_heuristics = [str(h) for h in data.get("governing_heuristics", []) if str(h).strip()]

    # Deterministic routing rule (never delegated to LLM)
    blocking = [a for a in assumptions if a.severity == "Blocking"]
    routing = _compute_routing(
        composite, skeptic_score, blocking, _load_thresholds(context.product_id)
    )

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
        governing_heuristics=governing_heuristics,
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

    store.save_artifact(
        run_id=context.run_id,
        artifact_type="checkpoint",
        content_md=_build_checkpoint(stage_input, output_data),
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


def _build_checkpoint(stage_input: S5Input, data: S5OutputData) -> str:
    routing_label = data.routing.upper() if hasattr(data.routing, "upper") else str(data.routing).upper()

    persona_rows = "\n".join(
        f"| {p.persona.capitalize()} | {p.dimension} | {p.score}/5 |"
        for p in stage_input.s4_output.personas
    )

    blocking = [a for a in data.assumptions if a.severity == "Blocking"]
    blocking_lines = "\n".join(f"- {a.statement}" for a in blocking) if blocking else "None"

    next_action = {
        "kill": "Pipeline complete — opportunity killed.",
        "poc": "Next: S6A PoC Plan → S7 Executive Summary",
        "prd": "Next: S6B PRD → S7 Executive Summary",
    }.get(str(data.routing), "Next: continue pipeline")

    return f"""# Pipeline Checkpoint — S5 Complete

## Evaluation Summary
| Persona | Dimension | Score |
|---------|-----------|-------|
{persona_rows}

**Composite Score:** {data.composite_score}/5.00

## Decision
**Routing: {routing_label}**

**Blocking Assumptions:**
{blocking_lines}

**Rationale:** {data.rationale}

---
**{next_action}**
"""


def _build_decision_memo(data: S5OutputData) -> str:
    routing_label = data.routing.upper() if hasattr(data.routing, "upper") else str(data.routing).upper()

    blocking = [a for a in data.assumptions if a.severity == "Blocking"]
    adjusting = [a for a in data.assumptions if a.severity != "Blocking"]

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

## Adjusting Assumptions
{_fmt_assumptions(adjusting)}

## Rationale
{data.rationale}

## Governing Heuristics
{", ".join(data.governing_heuristics) if data.governing_heuristics else "None cited"}
"""


def _compute_routing(
    composite: float,
    confidence: int,
    blocking: list[Assumption],
    thresholds: dict | None = None,
) -> str:
    """Deterministic two-axis hybrid routing — not delegated to the LLM.

    Composite answers "how good is this overall?" and sets the quality floor
    (kill and PRD-eligibility). Confidence answers "do we know enough to commit?"
    and alone decides prd vs poc for PRD-eligible opportunities — high scores
    elsewhere must never let an unvalidated opportunity skip validation.

    Canonical rule documentation: pm-decision-context/core/04-scoring.md
    ("Routing Decision — Two-Axis Hybrid Rule"). Change them together.
    """
    t = thresholds or _DEFAULT_THRESHOLDS
    if blocking or composite <= t["kill_threshold"]:
        return "kill"
    if composite >= t["prd_threshold"] and confidence >= t["confidence_gate"]:
        return "prd"
    return "poc"


def _resolve_model() -> str:
    from config import settings

    if settings.LLM_PROVIDER.lower() == "claude":
        return settings.ANTHROPIC_MODEL
    return settings.OPENAI_MODEL
