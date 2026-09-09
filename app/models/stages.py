from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.llm.protocol import Usage
from app.models.decision_case import DecisionCase

# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


class RunContext(BaseModel):
    """Immutable context passed into every stage function."""

    run_id: str = Field(description="UUID of the WorkflowRun")
    product_id: str = Field(description="Matches products/<name>/ directory name")
    pm_identity: str = Field(description="Full text of core/00-pm-identity.md")
    company_context: str = Field(description="Full text of company-context.md")
    product_context: str = Field(description="Full text of products/<name>/context.md")
    # New decision-request runs pin this separately from legacy S1/S2 summaries.
    # Historical runs omit it and retain their exact former context shape.
    decision_case: DecisionCase | None = Field(default=None)
    # The telemetry session of whoever started the run, carried here so every
    # stage span can name it without re-reading the run (telemetry plan P3).
    # None whenever the caller was not traced.
    origin_trace_id: str | None = Field(default=None)
    decision_pipeline_version: Literal["legacy", "evidence_v1"] = "legacy"


class StageMetadata(BaseModel):
    """Execution metadata attached to every stage output."""

    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    model_used: str | None = Field(
        default=None, description="LLM model identifier, or null if no LLM was called"
    )
    input_tokens: int | None = Field(
        default=None,
        description=(
            "Prompt tokens for this stage (sum across all LLM calls incl. "
            "JSON-repair retries), or null if no LLM was called"
        ),
    )
    output_tokens: int | None = Field(
        default=None,
        description=(
            "Completion tokens for this stage (sum across all LLM calls), "
            "or null if no LLM was called"
        ),
    )

    @classmethod
    def with_usage(
        cls, model: str | None, usage_sink: list[Usage] | None
    ) -> StageMetadata:
        """Build metadata from a model id and a usage_sink (list of {input_tokens, output_tokens}).

        Sums the sink so multiple calls in one stage (e.g. JSON-repair retries, or
        S4's parallel persona calls aggregated upstream) roll into the stage total.
        An empty/None sink leaves the token fields null.
        """
        inp: int | None
        out: int | None
        if usage_sink:
            inp = sum(u.get("input_tokens", 0) for u in usage_sink)
            out = sum(u.get("output_tokens", 0) for u in usage_sink)
        else:
            inp = out = None
        return cls(model_used=model, input_tokens=inp, output_tokens=out)


# ---------------------------------------------------------------------------
# Stage 1 — Signal Ingestion
# ---------------------------------------------------------------------------


class S1Input(BaseModel):
    """Raw signal submitted by the user or collected automatically."""

    signal_id: str = Field(description="UUID from the signals table")
    title: str = Field(description="Short descriptive title for the signal")
    raw_content: str = Field(description="Full raw text of the signal")
    source_url: str | None = Field(default=None, description="Source URL if available")
    source_type: str = Field(default="manual", description="manual | rss | file_watch")


class S1OutputData(BaseModel):
    """Structured, normalized signal data produced by Stage 1."""

    signal_id: str = Field(description="UUID from the signals table")
    title: str = Field(description="Normalized title")
    summary: str = Field(description="Concise factual summary of the signal (facts only)")
    category: str = Field(description="competitor | platform | regulation | technology | other")
    source: str = Field(description="Publication, channel, or URL")
    event_date: str | None = Field(
        default=None, description="Date of the event (not the ingestion date)"
    )
    quality_passed: bool = Field(description="True if the signal passes Stage 1 quality gates")


class S1Output(BaseModel):
    """Full Stage 1 output envelope."""

    stage: Literal["s1"] = "s1"
    run_id: str
    version: int = 1
    output: S1OutputData
    metadata: StageMetadata


# ---------------------------------------------------------------------------
# Portfolio Triage (US-49) — cross-product relevance routing, pre-fan-out
# ---------------------------------------------------------------------------


class ProductRelevance(BaseModel):
    """One product's relevance verdict from Portfolio Triage."""

    product_id: str
    relevance_score: int = Field(ge=1, le=5, description="1–5 relevance to this product")
    reason: str = Field(description="One sentence grounded in the product's profile")
    relevant: bool = Field(
        default=False,
        description="Set by the engine: True iff relevance_score >= threshold",
    )


class PortfolioTriageOutput(BaseModel):
    """Result of routing one signal across the portfolio."""

    signal_id: str
    threshold: int = Field(description="Relevance cutoff applied to decide fan-out")
    products: list[ProductRelevance] = Field(default_factory=list)

    @property
    def relevant_product_ids(self) -> list[str]:
        return [p.product_id for p in self.products if p.relevant]


# ---------------------------------------------------------------------------
# Portfolio Synthesis (US-49, Variant 2) — post-hoc cross-product memo
# ---------------------------------------------------------------------------


class PortfolioPriorityItem(BaseModel):
    """One product's place in the portfolio priority ranking."""

    product_id: str
    rank: int = Field(ge=1, description="1 = highest portfolio priority")
    rationale: str


class PortfolioSynthesisData(BaseModel):
    """Structured cross-product reading produced by portfolio synthesis.

    Visibility only — does not change any product's routing.
    """

    priority_ranking: list[PortfolioPriorityItem] = Field(default_factory=list)
    shared_root_cause: str = Field(default="")
    sequencing: str = Field(default="")
    resource_conflicts: str = Field(default="")
    synergies: str = Field(default="")
    recommendation: str = Field(default="")


# ---------------------------------------------------------------------------
# Stage 2 — Insight Extraction
# ---------------------------------------------------------------------------


class S2Input(BaseModel):
    """Input for Stage 2: the structured S1 output plus product context."""

    signal_id: str
    s1_output: S1OutputData
    product_id: str


class ProvenancedClaim(BaseModel):
    """One claim in the insight, tagged with where it came from.

    Makes the Insight Memo auditable: a reader can tell, at a glance, a fact
    stated in the signal from a fact drawn from product context from the
    engine's own inference — and trace each inference back to the claims it
    rests on. Replaces the old free-text ``relevance_explanation`` (which
    blended all three provenances into one ungated paragraph).
    """

    text: str = Field(description="A single claim — one provenance only")
    source: Literal["signal", "product_context", "inference"] = Field(
        description=(
            "signal = stated in the signal itself; "
            "product_context = drawn from the product context (context.md); "
            "inference = derived by the engine's own reasoning"
        )
    )
    grounds: list[int] = Field(
        default_factory=list,
        description=(
            "Only for source=inference: 1-based positions of the claims in this "
            "list that this inference is derived from. Non-empty for inference "
            "claims; empty for signal/product_context claims."
        ),
    )


def flatten_claims(claims: object) -> str:
    """Join provenance-claim texts into one block.

    For raw-JSON (dict) readers that bypass the ``S2OutputData`` model and so
    cannot use the back-compat ``relevance_explanation`` property — e.g. the
    Gate 1 review payload assembled directly from stored ``output_json``.
    """
    if not isinstance(claims, list):
        return ""
    return "\n".join(c.get("text", "") for c in claims if isinstance(c, dict) and c.get("text"))


class S2OutputData(BaseModel):
    """Insights extracted by Stage 2."""

    what_changed: str = Field(
        description="Concrete description of what is different in the external world"
    )
    reframing: str = Field(
        description="Market framing vs the correct reframing for this product's segment"
    )
    pillar_references: list[str] = Field(
        description="Strategy pillars from context.md that this signal is relevant to"
    )
    claims: list[ProvenancedClaim] = Field(
        description=(
            "Why this signal matters for the specific product, broken into "
            "provenance-tagged claims so signal facts, product context, and "
            "engine inference stay distinguishable. Replaces relevance_explanation."
        )
    )
    relevance_score: int = Field(
        ge=1,
        le=5,
        description=(
            "Strategic relevance score 1–5. 1–2: noise; 3: borderline; 4–5: clearly relevant"
        ),
    )
    depth_basis: Literal[
        "no_product_surface", "trend_only", "named_gap", "options_exist", "commit_ready"
    ] = Field(
        default="trend_only",
        description=(
            "What the signal actually contains, decided BEFORE the depth and on "
            "different grounds from relevance: does it name something this product "
            "would have to answer? The depth follows from this."
        ),
    )
    suggested_mode: Literal["archive", "note", "structure", "evaluate", "decide"] = Field(
        description="Recommended processing depth (archive<note<structure<evaluate<decide)"
    )
    suggestion_reasoning: str = Field(
        description="One sentence explaining why this depth is appropriate"
    )

    @field_validator("suggested_mode", mode="before")
    @classmethod
    def _normalize_legacy_mode(cls, v: object) -> object:
        # file/brief/opportunity were renamed to archive/note/structure (US-43);
        # coerce legacy values from stored S2 outputs / stray LLM output.
        from app.modes import normalize_mode

        return normalize_mode(v) if isinstance(v, str) else v

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_relevance_explanation(cls, data: object) -> object:
        # Pre-claims runs (and older mocked LLM output / test fixtures) carry a
        # free-text `relevance_explanation` instead of `claims`. Fold it into a
        # single inference claim so old stored S2 outputs stay constructible
        # without a destructive migration. New output supplies `claims` directly.
        if isinstance(data, dict) and not data.get("claims"):
            legacy = data.get("relevance_explanation")
            if isinstance(legacy, str) and legacy.strip():
                return {**data, "claims": [{"text": legacy, "source": "inference"}]}
        return data

    @property
    def relevance_explanation(self) -> str:
        """Back-compat flattening: the claim texts joined as one block.

        Downstream stages, exporters, and notifiers still read a single
        "why it matters" string; the provenance tags live in `claims` and in
        the rendered Insight Memo. New code should prefer `claims`.
        """
        return "\n".join(c.text for c in self.claims)


class S2Output(BaseModel):
    """Full Stage 2 output envelope."""

    stage: Literal["s2"] = "s2"
    run_id: str
    version: int = 1
    output: S2OutputData
    metadata: StageMetadata


# ---------------------------------------------------------------------------
# Stage 3 — Opportunity Creation
# ---------------------------------------------------------------------------


class S3Input(BaseModel):
    """Input for Stage 3: the S2 insight output plus product context."""

    signal_id: str
    s2_output: S2OutputData
    product_id: str


class S3OutputData(BaseModel):
    """Structured opportunity framing produced by Stage 3."""

    problem_statement: str = Field(
        description="Specific problem for the target user, grounded in the insight"
    )
    target_user: str = Field(
        description="More specific than the broad user segment — role and context"
    )
    hypothesis: str = Field(description="Falsifiable: 'If we do X, then Y will happen, because Z'")
    assumed_value_user: str = Field(description="Specific benefit for the user")
    assumed_value_business: str = Field(description="Specific benefit for the business")
    value_horizon: Literal["durable", "transient"] = Field(
        default="durable",
        description=(
            "Is the opportunity's value durable (compounds / defensible) or "
            "transient (a closing window a vendor/competitor may erase)? Feeds the "
            "Gate 3 closing-window recommendation (US-41). See core/04-scoring.md "
            "'Value Horizon'. Default 'durable' (backward-compatible)."
        ),
    )


class S3Output(BaseModel):
    """Full Stage 3 output envelope."""

    stage: Literal["s3"] = "s3"
    run_id: str
    version: int = 1
    output: S3OutputData
    metadata: StageMetadata


def render_value_horizon(value_horizon: str) -> str:
    """Human-readable Value Horizon block, shared by the in-app opportunity memo
    and the archive markdown export so the two never drift.

    The bare ``durable``/``transient`` label is meaningless to a reader, so each
    is paired with the reason it matters — this is the only place a ``structure``-
    depth run (which never reaches S5) surfaces the S3 horizon judgment to a human.
    """
    vh = (value_horizon or "durable").lower()
    if vh == "transient":
        return (
            "**Transient** — a closing window; a platform vendor or competitor "
            "may erase this value (e.g. by shipping a native capability)."
        )
    return "**Durable** — value compounds and is defensible over time."


# ---------------------------------------------------------------------------
# Stage 4 — Persona Evaluation (4 parallel agents)
# ---------------------------------------------------------------------------


class S4Input(BaseModel):
    s3_output: S3OutputData
    feedback: str | None = Field(default=None, description="PM feedback injected on revise")
    version: int = Field(default=1, description="Increments on each revise cycle")


class PersonaOutput(BaseModel):
    """Output from a single persona agent."""

    persona: Literal["explorer", "strategist", "builder", "skeptic"] = Field(
        description="Which persona produced this evaluation"
    )
    dimension: str = Field(description="Scoring dimension owned by this persona")
    score: int = Field(ge=1, le=5, description="Score 1–5 for the owned dimension")
    key_argument: str = Field(description="2–4 sentence evaluation from this persona's lens")
    open_question: str = Field(description="Single most important open question")
    # E06 evidence_v1 fields. Defaults preserve immutable legacy S4 records.
    evidence_passage_ids: list[str] = Field(default_factory=list)
    option_assessments: dict[str, str] = Field(default_factory=dict)
    option_positions: dict[str, Literal["support", "oppose", "uncertain"]] = Field(
        default_factory=dict
    )
    uncertainties: list[str] = Field(default_factory=list)


class S4RubricResult(BaseModel):
    total_score: int = Field(description="Sum of 4 dimensions, max 12")
    score_grounding: int = Field(ge=1, le=3)
    skeptic_quality: int = Field(ge=1, le=3)
    open_question_quality: int = Field(ge=1, le=3)
    persona_independence: int = Field(ge=1, le=3)
    evidence_linkage_quality: int = Field(default=0, ge=0, le=3)
    uncertainty_quality: int = Field(default=0, ge=0, le=3)
    passed: bool = Field(description="True if total_score >= 9")
    issues: list[str] = Field(default_factory=list)


class OptionDisagreement(BaseModel):
    """Independent persona positions for one proposed option."""

    option: str
    positions: dict[str, Literal["support", "oppose", "uncertain"]]
    evidence_passage_ids: dict[str, list[str]]
    status: Literal["consensus", "disagreement", "insufficient_assessment"]


class DisagreementMatrix(BaseModel):
    """A deterministic summary; it never infers a disagreement from missing data."""

    options: list[OptionDisagreement] = Field(default_factory=list)
    material_disagreement_options: list[str] = Field(default_factory=list)


class S4OutputData(BaseModel):
    personas: list[PersonaOutput] = Field(
        description="All 4 persona outputs, order: explorer/strategist/builder/skeptic"
    )
    rubric: S4RubricResult
    disagreement_matrix: DisagreementMatrix | None = None


class S4Output(BaseModel):
    """Full Stage 4 output envelope."""

    stage: Literal["s4"] = "s4"
    run_id: str
    version: int = 1
    output: S4OutputData
    metadata: StageMetadata


# ---------------------------------------------------------------------------
# Stage 5 — Prioritization and Routing
# ---------------------------------------------------------------------------


class S5Input(BaseModel):
    s4_output: S4OutputData


class Assumption(BaseModel):
    statement: str = Field(description="The assumption being classified")
    severity: Literal["Blocking", "Adjusting"] = Field(
        description=(
            "Blocking gates WHETHER you build (if false, nothing worth building "
            "is left → Kill). Adjusting gates HOW you build (if false, a smaller "
            "or different version still survives). See core/04-scoring.md."
        )
    )
    reason: str = Field(description="Why this assumption is Blocking or Adjusting")

    @field_validator("severity", mode="before")
    @classmethod
    def _normalize_legacy_severity(cls, v: object) -> object:
        # "Informing" was renamed to "Adjusting" (2026-06-10). Coerce legacy
        # stored values and any stray LLM output so old runs stay readable
        # without a destructive DB migration.
        if isinstance(v, str) and v.strip().lower() == "informing":
            return "Adjusting"
        return v


class ReadinessFinding(BaseModel):
    category: Literal["evidence", "uncertainty", "disagreement", "blocking_assumption"]
    severity: Literal["Blocking", "Advisory"]
    message: str


class DecisionReadiness(BaseModel):
    """Decision-support status; this records gaps and never changes S5 routing."""

    ready_for_prd: bool
    provisional: bool
    findings: list[ReadinessFinding] = Field(default_factory=list)


class S5OutputData(BaseModel):
    impact_score: int = Field(ge=1, le=5)
    strategic_fit_score: int = Field(ge=1, le=5)
    feasibility_score: int = Field(ge=1, le=5)
    confidence_score: int = Field(ge=1, le=5)
    composite_score: float
    routing: Literal["prd", "poc", "kill"]
    assumptions: list[Assumption]
    rationale: str = Field(description="Narrative rationale for the routing decision")
    blocking_count: int
    governing_heuristics: list[str] = Field(
        default_factory=list,
        description=(
            "Decision heuristic numbers from core/00-pm-identity.md that governed "
            "this routing call (e.g. ['#7', '#14']). Makes the philosophy → "
            "principles → decision chain auditable. Empty if none cited."
        ),
    )
    closing_window: bool = Field(
        default=False,
        description=(
            "Value Horizon flag (US-41): the opportunity's value is transient AND "
            "Impact is high AND it is not Blocked — a closing window. Surfaced at "
            "Gate 3 to prompt a fast, time-boxed bet over the default track. Does "
            "NOT change routing. See core/04-scoring.md 'Value Horizon'."
        ),
    )
    readiness: DecisionReadiness | None = None


class S5Output(BaseModel):
    """Full Stage 5 output envelope."""

    stage: Literal["s5"] = "s5"
    run_id: str
    version: int = 1
    output: S5OutputData
    metadata: StageMetadata


# ---------------------------------------------------------------------------
# Stage 6A — PoC Plan
# ---------------------------------------------------------------------------


class S6AInput(BaseModel):
    s5_output: S5OutputData


class S6AOutputData(BaseModel):
    experiment_goal: str
    blocking_assumptions_addressed: list[str]
    experiment_design: str
    success_criteria: str
    timeline_weeks: int
    resources_needed: str


class S6AOutput(BaseModel):
    stage: Literal["s6a"] = "s6a"
    run_id: str
    version: int = 1
    output: S6AOutputData
    metadata: StageMetadata


# ---------------------------------------------------------------------------
# Stage 6B — PRD
# ---------------------------------------------------------------------------


class S6BInput(BaseModel):
    s5_output: S5OutputData


class PRDCompletenessCheck(BaseModel):
    problem_statement: bool
    target_user: bool
    hypothesis: bool
    success_metrics: bool
    user_stories: bool
    in_scope: bool
    out_of_scope: bool
    technical_dependencies: bool
    open_questions: bool
    non_goals: bool
    rollout_phases: bool
    risks: bool

    @property
    def score(self) -> int:
        return sum(1 for v in self.model_dump().values() if v)


class S6BOutputData(BaseModel):
    problem_statement: str
    target_user: str
    success_metrics: list[str]
    user_stories: list[str]
    in_scope: list[str]
    out_of_scope: list[str]
    technical_dependencies: list[str]
    open_questions: list[str]
    risks: list[str]
    completeness: PRDCompletenessCheck


class S6BOutput(BaseModel):
    stage: Literal["s6b"] = "s6b"
    run_id: str
    version: int = 1
    output: S6BOutputData
    metadata: StageMetadata


# ---------------------------------------------------------------------------
# Stage 7 — Executive Summary
# ---------------------------------------------------------------------------


class S7Input(BaseModel):
    mode: Literal["archive", "note", "structure", "evaluate", "decide"] = "decide"
    s5_output: S5OutputData | None = None
    s6a_output: S6AOutputData | None = None
    s6b_output: S6BOutputData | None = None

    @field_validator("mode", mode="before")
    @classmethod
    def _normalize_legacy_mode(cls, v: object) -> object:
        from app.modes import normalize_mode

        return normalize_mode(v) if isinstance(v, str) else v


class S7OutputData(BaseModel):
    what_we_saw: str = Field(description="S1/S2 summary — signal and insight")
    what_it_means: str = Field(description="S3 summary — opportunity framing")
    what_we_decided: str = Field(description="S4/S5 summary — evaluation and routing")
    what_we_will_do_next: str = Field(description="S6 summary — plan or PRD")
    markdown: str = Field(description="Full formatted executive summary as Markdown")


class S7Output(BaseModel):
    stage: Literal["s7"] = "s7"
    run_id: str
    version: int = 1
    output: S7OutputData
    metadata: StageMetadata
