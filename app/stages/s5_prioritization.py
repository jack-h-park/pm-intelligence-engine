"""Stage 5 — Prioritization and Routing.

Composite score is computed deterministically from S4 persona scores.
LLM classifies assumptions (Blocking / Adjusting) and writes a rationale.
Routing rule is deterministic code — never delegated to the LLM.
"""

from app.llm.json_call import complete_json
from app.llm.protocol import LLMProvider
from app.logging import emit_event
from app.models.stages import (
    Assumption,
    RunContext,
    S5Input,
    S5Output,
    S5OutputData,
    StageMetadata,
)
from app.services.decision_case import render_decision_case
from app.services.decision_readiness import assess_readiness
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

# Value Horizon (US-41): a transient opportunity is only worth a fast bet if the
# prize is large — gate the closing-window flag on Impact (explorer score).
_CLOSING_WINDOW_IMPACT_MIN = 4


def _value_horizon_from_store(store: PMWorkflowStore, run_id: str) -> str:
    """Read the S3-judged value_horizon for this run. Defaults to 'durable'
    (backward-compatible) if S3 output is absent or lacks the field."""
    import json

    raw = store.get_stage_output(run_id, "s3")
    if raw is None:
        return "durable"
    try:
        return json.loads(raw["output_json"])["output"].get("value_horizon", "durable")
    except Exception:  # noqa: BLE001
        return "durable"


def _load_scoring_config(product_id: str) -> dict:
    """Merged scoring config for a product: git baseline, then runtime override.

    The baseline is {DECISION_CONTEXT_ROOT}/products/{id}/scoring.yaml, which is
    version-controlled and stays the declared intent. SCORING_OVERRIDES_FILE, if
    configured and present, layers per-product keys on top — that is how PM
    Observatory retunes a weight without a commit.

    Returns {} when neither source yields a usable mapping, which makes every
    caller fall back to its defaults exactly as before this existed.
    """
    import json
    import pathlib

    import yaml

    from config import settings

    merged: dict = {}

    path = pathlib.Path(settings.DECISION_CONTEXT_ROOT) / "products" / product_id / "scoring.yaml"
    if path.exists():
        try:
            raw = yaml.safe_load(path.read_text())
            if isinstance(raw, dict):
                merged.update(raw)
        except Exception:  # noqa: BLE001 — a malformed baseline must not stop S5
            pass

    override_path = settings.SCORING_OVERRIDES_FILE
    if override_path:
        try:
            data = json.loads(pathlib.Path(override_path).read_text())
            product = data.get(product_id) if isinstance(data, dict) else None
            if isinstance(product, dict):
                merged.update(product)
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001 — same posture as the baseline
            pass

    return merged


def _load_weights(product_id: str) -> dict:
    """Per-product S4-persona weights, normalised to sum to 1.0.

    Falls back to _DEFAULT_WEIGHTS unless all four keys are present and positive
    — a partial set is ambiguous about what the missing ones should be, so it is
    rejected rather than half-applied.

    Config keys:
      impact: 0.35
      strategic_fit: 0.30
      feasibility: 0.20
      confidence: 0.15
    """
    raw = _load_scoring_config(product_id)
    if not raw:
        return _DEFAULT_WEIGHTS.copy()

    try:
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
    """Per-product routing thresholds, from the same merged config as the weights.

    Unlike the weights, these fall back key by key: each threshold is independent,
    so a config that sets only `kill_threshold` means exactly that and the rest
    keep their defaults.

    Keys (all optional):
      kill_threshold: 1.5
      prd_threshold: 3.5
      confidence_gate: 4
    """
    raw = _load_scoring_config(product_id)
    if not raw:
        return _DEFAULT_THRESHOLDS.copy()

    try:
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

## Pinned Decision Case
{render_decision_case(context.decision_case)}

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

    usage_sink: list = []
    data = await complete_json(
        llm,
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ],
        stage="s5",
        run_id=context.run_id,
        usage_sink=usage_sink,
        max_tokens=1024,
        temperature=0,  # deterministic — blocking classification must be consistent
    )
    assumptions = [Assumption(**a) for a in data.get("assumptions", [])]
    rationale = data.get("rationale", "")
    governing_heuristics = [str(h) for h in data.get("governing_heuristics", []) if str(h).strip()]

    # Blocking-assumption verifier (US-42): re-apply the strict two-question test
    # to each Blocking and downgrade over-eager ones to Adjusting before routing.
    blocking = [a for a in assumptions if a.severity == "Blocking"]
    if settings.BLOCKING_VERIFIER_ENABLED and blocking:
        downgrades = await _verify_blocking_assumptions(
            blocking, persona_summary, system_message, llm, context.run_id, usage_sink=usage_sink
        )
        for a in assumptions:
            if a.severity == "Blocking" and a.statement in downgrades:
                a.severity = "Adjusting"
                emit_event(
                    "s5",
                    "blocking_downgraded",
                    context.run_id,
                    {"statement": a.statement, "reason": downgrades[a.statement]},
                )
        blocking = [a for a in assumptions if a.severity == "Blocking"]

    # Deterministic routing rule (never delegated to LLM)
    routing = _compute_routing(
        composite, skeptic_score, blocking, _load_thresholds(context.product_id)
    )

    # Value Horizon (US-41): flag a closing window — transient value + high Impact
    # + not Blocked — so Gate 3 can prompt a fast time-boxed bet. Does NOT change
    # routing; the PM decides. value_horizon is judged in S3.
    impact_score = scores.get("explorer", 3)
    closing_window = (
        _value_horizon_from_store(store, context.run_id) == "transient"
        and impact_score >= _CLOSING_WINDOW_IMPACT_MIN
        and not blocking
    )
    if closing_window:
        emit_event(
            "s5",
            "closing_window_flagged",
            context.run_id,
            {"impact": impact_score, "routing": routing},
        )

    output_data = S5OutputData(
        impact_score=impact_score,
        strategic_fit_score=scores.get("strategist", 3),
        feasibility_score=scores.get("builder", 3),
        confidence_score=skeptic_score,
        composite_score=composite,
        routing=routing,
        assumptions=assumptions,
        rationale=rationale,
        blocking_count=len(blocking),
        governing_heuristics=governing_heuristics,
        closing_window=closing_window,
        readiness=(
            assess_readiness(stage_input.s4_output, routing, assumptions)
            if context.decision_pipeline_version == "evidence_v1"
            else None
        ),
    )

    output = S5Output(
        run_id=context.run_id,
        output=output_data,
        metadata=StageMetadata.with_usage(_resolve_model(), usage_sink),
    )

    store.save_stage_output(
        run_id=context.run_id,
        stage="s5",
        output_json=output.model_dump_json(),
    )

    store.save_artifact(
        run_id=context.run_id,
        artifact_type="decision_memo",
        content_md=build_decision_memo(output_data),
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



def build_decision_memo(data: S5OutputData) -> str:
    routing_label = data.routing.upper() if hasattr(data.routing, "upper") else str(data.routing).upper()

    blocking = [a for a in data.assumptions if a.severity == "Blocking"]
    adjusting = [a for a in data.assumptions if a.severity != "Blocking"]

    def _fmt_assumptions(items: list[Assumption]) -> str:
        if not items:
            return "None"
        return "\n".join(f"- {a.statement}" for a in items)

    readiness_md = ""
    if data.readiness is not None:
        state = "Ready for PRD" if data.readiness.ready_for_prd else "Provisional — unresolved gaps remain"
        findings = "\n".join(
            f"- **{finding.severity} · {finding.category}:** {finding.message}"
            for finding in data.readiness.findings
        ) or "- No unresolved evidence_v1 readiness gaps."
        readiness_md = f"\n## Decision Readiness\n**{state}**\n{findings}\n"

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
{readiness_md}
{"" if not data.closing_window else chr(10) + "## ⏳ Closing Window" + chr(10) + "Value is transient and Impact is high — consider a fast, time-boxed bet over the default track."}
"""


_VERIFIER_JSON_SCHEMA = """{
  "verdicts": [
    {"statement": "<the blocking assumption, copied verbatim>", "keep_blocking": true, "reason": "<why no alternative path exists, or why an alternative path exists>"}
  ]
}"""


async def _verify_blocking_assumptions(
    blocking: list[Assumption],
    persona_summary: str,
    system_message: str,
    llm: LLMProvider,
    run_id: str,
    usage_sink: list | None = None,
) -> dict:
    """Adversarially audit Blocking classifications (US-42).

    Returns {statement: reason} for assumptions that should be DOWNGRADED to
    Adjusting because a plausible alternative path to the value exists. Blocking
    is rare; this corrects over-eager Blocking flags. It does NOT touch the
    time-horizon/magnitude axis (that is separate, future Part B work).
    """
    listing = "\n".join(
        f"{i+1}. {a.statement} — claimed reason: {a.reason}" for i, a in enumerate(blocking)
    )
    user_message = f"""You are auditing **Blocking** assumption classifications from a prior step. Blocking must be RARE.

An assumption is **Blocking** ONLY if BOTH hold:
  (1) the opportunity is worthless if the assumption is false, AND
  (2) NO alternative path, workaround, or fallback to the same value exists in the evidence below.
If a plausible alternative path exists, it is NOT Blocking — it is **Adjusting** (keep_blocking = false).
Do not consider long-term/timing erosion here — only "is there an alternative path to the value right now?".

## Evidence (persona arguments)
{persona_summary}

## Blocking assumptions to audit
{listing}

For each assumption, set keep_blocking = true only if there is genuinely NO alternative path.
When a plausible alternative path exists, set keep_blocking = false (downgrade to Adjusting).
Respond with a single JSON object — no markdown, no commentary:

{_VERIFIER_JSON_SCHEMA}"""

    data = await complete_json(
        llm,
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ],
        stage="s5",
        run_id=run_id,
        usage_sink=usage_sink,
        max_tokens=1024,
        temperature=0,
    )
    blocking_statements = {a.statement for a in blocking}
    downgrades: dict = {}
    for v in data.get("verdicts", []):
        stmt = v.get("statement", "")
        if stmt in blocking_statements and not v.get("keep_blocking", True):
            downgrades[stmt] = v.get("reason", "")
    return downgrades


def _compute_routing(
    composite: float,
    confidence: int,
    blocking: list[Assumption],
    thresholds: dict | None = None,
) -> str:
    """Deterministic two-axis hybrid routing — not delegated to the LLM.

    Two axes:
      - Composite = "is this worth doing?" (value). A composite at/below the kill
        floor is a genuinely low-value opportunity → kill.
      - Confidence + open Blocking assumptions = "do we know enough to commit?"
        A Blocking assumption is an *unresolved question* whose answer gates the
        opportunity — that is the definition of a PoC candidate (go validate it),
        NOT a reason to kill. A true value-nullifier ("even if built, no one wants
        it") already shows up as a low composite and is caught by the kill floor
        above; so Blocking routes to **poc**, not kill.

    Ordering matters: the kill floor is checked first, so a low-value opportunity
    that also has blockers still kills (R06: composite 1.35 + 3 Blocking → kill on
    the value floor). A decent-value opportunity with unresolved blockers goes to
    poc (run 9b49fc8c: composite 3.6 + Blocking → poc, where the old
    `blocking → kill` rule wrongly killed it and needed a manual Gate 3 override).

    Canonical rule documentation: pm-decision-context/core/04-scoring.md
    ("Routing Decision — Two-Axis Hybrid Rule"). Change them together.
    """
    t = thresholds or _DEFAULT_THRESHOLDS
    if composite <= t["kill_threshold"]:
        return "kill"
    if blocking:
        return "poc"
    if composite >= t["prd_threshold"] and confidence >= t["confidence_gate"]:
        return "prd"
    return "poc"


def _resolve_model() -> str:
    from config import settings

    if settings.LLM_PROVIDER.lower() == "claude":
        return settings.ANTHROPIC_MODEL
    return settings.OPENAI_MODEL
