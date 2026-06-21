import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, Column, DateTime, Enum as SAEnum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class RunStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    waiting_direction = "waiting_direction"
    waiting_approval = "waiting_approval"
    waiting_routing_review = "waiting_routing_review"
    completed = "completed"
    killed = "killed"
    failed = "failed"

    @classmethod
    def _missing_(cls, value):
        # Accept the legacy 'awaiting_direction' value (renamed → 'waiting_direction'
        # for prefix consistency; state glossary 2026-06-14) so old in-code calls
        # and pre-migration DB reads resolve. DB rows are migrated in
        # SQLiteStore._migrate_schema.
        if value == "awaiting_direction":
            return cls.waiting_direction
        return None


class RunMode(str, enum.Enum):
    # Processing-depth ladder (US-43). See app/modes.py and
    # pm-decision-context/core/02-workflow.md.
    archive = "archive"        # depth 1: S1 only — set aside, not pursued (was "file")
    note = "note"              # depth 2: + S2 (+ S7) — record the insight (was "brief")
    structure = "structure"    # depth 3: + S3 — structure the opportunity (was "opportunity")
    evaluate = "evaluate"      # depth 4: + S4 — full persona evaluation, no routing
    decide = "decide"          # depth 5: S1–S7 — full pipeline with gates

    @classmethod
    def _missing_(cls, value):
        # Accept legacy mode strings (file/brief/opportunity) so old in-code
        # RunMode(<legacy>) calls resolve. DB rows are migrated separately.
        from app.modes import _LEGACY_MODE_ALIASES
        alias = _LEGACY_MODE_ALIASES.get(value)
        return cls(alias) if alias is not None else None


class Routing(str, enum.Enum):
    prd = "prd"
    poc = "poc"
    kill = "kill"


class SignalCategory(str, enum.Enum):
    competitor = "competitor"
    platform = "platform"
    regulation = "regulation"
    technology = "technology"
    other = "other"


class SignalStatus(str, enum.Enum):
    new = "new"
    in_run = "in_run"
    done = "done"
    # A signal whose runs failed repeatedly (attempt_no reached MAX_RUN_ATTEMPTS).
    # Deliberately NOT returned to the retryable `new` pool — it stays out of the
    # auto-retry loop until a human investigates. Distinct from `done` (which
    # means a run reached a real decision). 7 chars — fits signals.status VARCHAR(7).
    blocked = "blocked"

    @classmethod
    def _missing_(cls, value):
        # Accept the legacy 'pending' value (renamed → 'new'; state glossary
        # 2026-06-14) so old in-code calls and pre-migration DB reads resolve.
        # `pending` collided with RunStatus.pending and the Gate 0 intake state.
        if value == "pending":
            return cls.new
        return None


class SourceType(str, enum.Enum):
    manual = "manual"
    rss = "rss"
    file_watch = "file_watch"
    web = "web"


class ApprovalAction(str, enum.Enum):
    approve = "approve"
    revise = "revise"
    reject = "reject"
    auto_triaged = "auto_triaged"  # system decision at the relevance gate (US-31)
    reopen = "reopen"              # PM revives an auto-triaged run (US-31)
    direction = "direction"        # PM mode choice at Gate 1 (US-44)
    confirm = "confirm"            # PM confirms S5 routing at Gate 3 (US-44)
    override = "override"          # PM overrides S5 routing at Gate 3 (US-44)


class ArtifactType(str, enum.Enum):
    poc_plan = "poc_plan"
    prd = "prd"
    executive_summary = "executive_summary"
    insight_memo = "insight_memo"
    opportunity_memo = "opportunity_memo"
    evaluation_brief = "evaluation_brief"
    decision_memo = "decision_memo"
    checkpoint = "checkpoint"


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Signal(Base):
    __tablename__ = "signals"

    signal_id = Column(String, primary_key=True, default=_new_uuid)
    # Provenance/origin hint, not an authoritative binding (US-49). NULL for
    # product-agnostic intake (RSS / file_watch) — Portfolio Triage routes those.
    # Authority over product routing lives in the spawned runs' product_id.
    original_product_id = Column(String, nullable=True)
    title = Column(String, nullable=False)
    source_url = Column(String, nullable=True)
    # Authoritative back-link to the originating intake artifact (the Hermes
    # sensing filename, e.g. "2026-05-28-anthropic-glasswing-initial-update.md").
    # NULL for signals not submitted through Gate 0 (manual POST, replays). When
    # present it is the deterministic join key the observatory uses to pair a
    # sensing file with its run — replacing the fragile fuzzy title match. See
    # gate0-state.json `submitted[].signal_id` for the reverse direction.
    source_ref = Column(String, nullable=True)
    raw_content = Column(Text, nullable=False)
    category = Column(SAEnum(SignalCategory), nullable=False, default=SignalCategory.other)
    status = Column(SAEnum(SignalStatus), nullable=False, default=SignalStatus.new)
    source_type = Column(SAEnum(SourceType), nullable=False, default=SourceType.manual)
    ingested_at = Column(DateTime, nullable=False, default=_utc_now)

    runs = relationship("WorkflowRun", back_populates="signal")


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    run_id = Column(String, primary_key=True, default=_new_uuid)
    product_id = Column(String, nullable=False)
    signal_id = Column(String, ForeignKey("signals.signal_id"), nullable=False)
    # Groups the runs created from one signal fan-out (US-49). NULL for legacy
    # single runs — treated as a batch of one (no portfolio synthesis).
    batch_id = Column(String, nullable=True)
    status = Column(SAEnum(RunStatus), nullable=False, default=RunStatus.pending)
    current_stage = Column(String, nullable=True)
    mode = Column(SAEnum(RunMode), nullable=True)
    recommendation_json = Column(Text, nullable=True)  # S2 suggested_mode + reasoning
    routing = Column(SAEnum(Routing), nullable=True)
    composite_score = Column(Float, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utc_now)
    # Bumped on every change (US: gate-watcher dedup needs to detect a run
    # re-entering a gate state, e.g. waiting_approval after a Gate 2 revise).
    updated_at = Column(DateTime, nullable=False, default=_utc_now, onupdate=_utc_now)
    completed_at = Column(DateTime, nullable=True)
    # Per-run LLM token totals (Phase 2), summed across all stage outputs at
    # finalize. NULL for runs that predate this column or never called an LLM.
    prompt_tokens_total = Column(Integer, nullable=True)
    completion_tokens_total = Column(Integer, nullable=True)

    # --- Retry lineage & failure diagnostics ---
    # A failed run returns its signal to the retryable pool, and the next pickup
    # creates a *brand new* run rather than resuming this one (by design — runs
    # are immutable attempts). These columns make that lineage explicit so the
    # observatory can collapse retries to one logical run, and so the DB (not
    # just server.log) can answer "why / where did it fail, and is it looping?".
    #
    # attempt_no: 1 for the first run of a (signal_id, product_id) lineage,
    #   incremented for each subsequent re-run. Computed in create_run.
    # root_run_id: run_id of attempt 1 in the lineage. NULL on attempt 1 itself
    #   (it *is* the root) — consumers use COALESCE(root_run_id, run_id) as the
    #   stable lineage key.
    attempt_no = Column(Integer, nullable=False, default=1)
    root_run_id = Column(String, nullable=True)
    # Set only on failure: the stage that was executing when the run died, and
    # the exception string. Both NULL for non-failed runs.
    failed_stage = Column(String, nullable=True)
    error = Column(Text, nullable=True)

    signal = relationship("Signal", back_populates="runs")
    stage_outputs = relationship("StageOutput", back_populates="run")
    approval_events = relationship("ApprovalEvent", back_populates="run")
    artifacts = relationship("Artifact", back_populates="run")


class StageOutput(Base):
    __tablename__ = "stage_outputs"

    output_id = Column(String, primary_key=True, default=_new_uuid)
    run_id = Column(String, ForeignKey("workflow_runs.run_id"), nullable=False)
    stage = Column(String, nullable=False)
    output_json = Column(Text, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, nullable=False, default=_utc_now)

    run = relationship("WorkflowRun", back_populates="stage_outputs")


class ApprovalEvent(Base):
    __tablename__ = "approval_events"

    event_id = Column(String, primary_key=True, default=_new_uuid)
    run_id = Column(String, ForeignKey("workflow_runs.run_id"), nullable=False)
    stage = Column(String, nullable=False)
    action = Column(SAEnum(ApprovalAction), nullable=False)
    feedback_text = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utc_now)

    run = relationship("WorkflowRun", back_populates="approval_events")


class Artifact(Base):
    __tablename__ = "artifacts"

    artifact_id = Column(String, primary_key=True, default=_new_uuid)
    run_id = Column(String, ForeignKey("workflow_runs.run_id"), nullable=False)
    type = Column(SAEnum(ArtifactType), nullable=False)
    content_md = Column(Text, nullable=False)
    content_json = Column(Text, nullable=False)
    source_stage = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utc_now)

    run = relationship("WorkflowRun", back_populates="artifacts")


class RunBatch(Base):
    """A fan-out batch: the set of runs created from one signal (US-49).

    `membership_closed` guards the portfolio-synthesis trigger. Membership is
    dynamic — the manual Portfolio Scan gate can add runs after the first run
    has started — so synthesis must not fire while more runs might still join.
    """

    __tablename__ = "run_batches"

    batch_id = Column(String, primary_key=True, default=_new_uuid)
    signal_id = Column(String, ForeignKey("signals.signal_id"), nullable=False)
    membership_closed = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=_utc_now)


class PortfolioSynthesis(Base):
    """Post-hoc cross-product memo for one fan-out batch (US-49, Variant 2).

    One synthesis per batch (batch_id is the PK — the insert-or-skip idempotency
    guard). Batch/signal-scoped, so deliberately not an Artifact row (which is
    run-scoped).
    """

    __tablename__ = "portfolio_syntheses"

    batch_id = Column(String, primary_key=True)
    signal_id = Column(String, ForeignKey("signals.signal_id"), nullable=False)
    content_md = Column(Text, nullable=False)
    content_json = Column(Text, nullable=False)
    run_ids_json = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=_utc_now)
