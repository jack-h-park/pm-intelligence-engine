import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# RunStatus (the legacy status enum) was removed in US-55 step 7d-2 — run state is
# the canonical (lifecycle, position, outcome, reason) columns; the status /
# current_stage columns are dropped by SQLiteStore._migrate.


class RunMode(enum.StrEnum):
    # Processing-depth ladder (US-43). See app/modes.py and
    # pm-decision-context/core/02-workflow.md.
    archive = "archive"  # depth 1: S1 only — set aside, not pursued (was "file")
    note = "note"  # depth 2: + S2 (+ S7) — record the insight (was "brief")
    structure = "structure"  # depth 3: + S3 — structure the opportunity (was "opportunity")
    evaluate = "evaluate"  # depth 4: + S4 — full persona evaluation, no routing
    decide = "decide"  # depth 5: S1–S7 — full pipeline with gates

    @classmethod
    def _missing_(cls, value: object) -> "RunMode | None":
        # Accept legacy mode strings (file/brief/opportunity) so old in-code
        # RunMode(<legacy>) calls resolve. DB rows are migrated separately.
        from app.modes import _LEGACY_MODE_ALIASES

        if not isinstance(value, str):
            return None
        alias = _LEGACY_MODE_ALIASES.get(value)
        return cls(alias) if alias is not None else None


class Routing(enum.StrEnum):
    prd = "prd"
    poc = "poc"
    kill = "kill"


class SignalCategory(enum.StrEnum):
    competitor = "competitor"
    platform = "platform"
    regulation = "regulation"
    technology = "technology"
    other = "other"


class SignalStatus(enum.StrEnum):
    new = "new"
    in_run = "in_run"
    done = "done"
    # A signal whose runs failed repeatedly (attempt_no reached MAX_RUN_ATTEMPTS).
    # Deliberately NOT returned to the retryable `new` pool — it stays out of the
    # auto-retry loop until a human investigates. Distinct from `done` (which
    # means a run reached a real decision). 7 chars — fits signals.status VARCHAR(7).
    blocked = "blocked"

    @classmethod
    def _missing_(cls, value: object) -> "SignalStatus | None":
        # Accept the legacy 'pending' value (renamed → 'new'; state glossary
        # 2026-06-14) so old in-code calls and pre-migration DB reads resolve.
        # `pending` collided with RunStatus.pending and the Gate 0 intake state.
        if value == "pending":
            return cls.new
        return None


class SourceType(enum.StrEnum):
    manual = "manual"
    rss = "rss"
    file_watch = "file_watch"
    web = "web"


class ApprovalAction(enum.StrEnum):
    approve = "approve"
    revise = "revise"
    reject = "reject"
    auto_triaged = "auto_triaged"  # system decision at the relevance gate (US-31)
    reopen = "reopen"  # PM revives an auto-triaged run (US-31)
    direction = "direction"  # PM mode choice at Gate 1 (US-44)
    confirm = "confirm"  # PM confirms S5 routing at Gate 3 (US-44)
    override = "override"  # PM overrides S5 routing at Gate 3 (US-44)
    void = "void"  # PM voids an improperly-started run (any non-terminal state)
    deepen = "deepen"  # PM resumes a completed run at a deeper depth (human-pull)
    preset = "preset"  # depth stated at run start, before Gate 1 existed for
    # this run. A real decision — the caller passed a depth
    # and S2's relevance score is explicitly overridden — so
    # it belongs in the audit log. Distinct from `direction`
    # for the same reason as `timeout` below: the choice was
    # made BEFORE S2 produced a suggestion, so scoring it as
    # agreement-with-the-suggestion measures nothing. The
    # suggestion is still recorded in the feedback text, for
    # anyone asking the different question of whether the
    # preset matched what S2 would have said.
    timeout = "timeout"  # gate1-timeout advanced a run the PM did not answer.
    # Distinct from `direction` so the row says WHO decided:
    # `reopen` can revive it (a PM decision it never can),
    # and Gate 1 agreement can exclude it — the job advances
    # at S2's own suggested depth, so it agrees by
    # construction and would otherwise read as the PM.


class ArtifactType(enum.StrEnum):
    poc_plan = "poc_plan"
    prd = "prd"
    executive_summary = "executive_summary"
    insight_memo = "insight_memo"
    opportunity_memo = "opportunity_memo"
    evaluation_brief = "evaluation_brief"
    decision_memo = "decision_memo"


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Signal(Base):
    __tablename__ = "signals"

    signal_id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    # Provenance/origin hint, not an authoritative binding (US-49). NULL for
    # product-agnostic intake (RSS / file_watch) — Portfolio Triage routes those.
    # Authority over product routing lives in the spawned runs' product_id.
    original_product_id: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)
    # Authoritative back-link to the originating intake artifact (the ops-plane
    # sensing filename, e.g. "2026-05-28-anthropic-glasswing-initial-update.md").
    # NULL for signals not submitted through Gate 0 (manual POST, replays). When
    # present it is the deterministic join key the observatory uses to pair a
    # sensing file with its run — replacing the fragile fuzzy title match. See
    # gate0-state.json `submitted[].signal_id` for the reverse direction.
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_content: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[SignalCategory] = mapped_column(
        SAEnum(SignalCategory), nullable=False, default=SignalCategory.other
    )
    status: Mapped[SignalStatus] = mapped_column(
        SAEnum(SignalStatus), nullable=False, default=SignalStatus.new
    )
    source_type: Mapped[SourceType] = mapped_column(
        SAEnum(SourceType), nullable=False, default=SourceType.manual
    )
    ingested_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now)
    # Set when a signal's raw_content is re-ingested via POST /signals/{id}/refresh
    # (e.g. the original crawl captured only site-chrome and a better fetch
    # recovered the article). NULL for signals that were never refreshed. The
    # content is otherwise immutable after Gate 0 intake.
    refreshed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    runs: Mapped[list["WorkflowRun"]] = relationship(back_populates="signal")
    notes: Mapped[list["SignalNote"]] = relationship(back_populates="signal")
    tags: Mapped[list["SignalTag"]] = relationship(back_populates="signal")


class SignalNote(Base):
    """A free-text review note on a signal — the human/agent record of *why* a
    signal was judged the way it was at a point in time.

    APPEND-ONLY BY DESIGN. Notes are never edited or deleted in place. The value
    here is the trail of judgment, not a current-state field: overwriting a note
    would erase the fact that an assessment changed, which is exactly the
    information that is hardest to reconstruct afterwards. It also closes a
    structural hazard — cron-driven agents write to this table far more often
    than a human does, so a mutable note would let a sweep silently clobber a
    hand-written one. ``author`` keeps the two distinguishable.

    Tags (``SignalTag``) are the mutable counterpart: they describe what a signal
    *is now*, so they are a set that is added to and removed from.

    ``superseded_by`` is reserved and currently always NULL — no endpoint writes
    it. It exists so that retracting/correcting a note can later be added as a
    forward pointer to a replacement note (preserving both) rather than as an
    UPDATE, without needing a second migration on the always-on DB.
    """

    __tablename__ = "signal_notes"

    note_id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    signal_id: Mapped[str] = mapped_column(String, ForeignKey("signals.signal_id"), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Who wrote it — "jack" for a human note, an agent label ("ops") for an
    # automated one. Required: an unattributed judgment record is near-useless.
    author: Mapped[str] = mapped_column(String, nullable=False)
    # Where in the lifecycle the note was captured (gate0 / triage / gate1 /
    # terminal / manual). Free-form on purpose — the capture points are still
    # settling; promote to an enum once they stop moving.
    context: Mapped[str | None] = mapped_column(String, nullable=True)
    # The run being reviewed, when the note was occasioned by one. NULL for notes
    # about the signal itself (e.g. a Gate 0 intake rationale, before any run).
    run_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("workflow_runs.run_id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now)
    superseded_by: Mapped[str | None] = mapped_column(String, nullable=True)

    signal: Mapped["Signal"] = relationship(back_populates="notes")


class SignalTag(Base):
    """A label on a signal. Mutable set semantics — add and remove.

    The vocabulary is deliberately NOT an enum: it is normalised (lowercase
    kebab) and bounded in size, but any tag string is accepted. Fixing a
    vocabulary before observing which labels are actually used in practice would
    freeze it around guesses; ``policies/signal-tag-vocabulary.md`` in the
    control plane is the (advisory) SSOT, and the enum can follow once real usage
    has accumulated.

    Tags are orthogonal to ``Signal.status`` — lifecycle state is never expressed
    as a tag.
    """

    __tablename__ = "signal_tags"

    signal_id: Mapped[str] = mapped_column(
        String, ForeignKey("signals.signal_id"), primary_key=True
    )
    tag: Mapped[str] = mapped_column(String, primary_key=True)
    author: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now)

    signal: Mapped["Signal"] = relationship(back_populates="tags")


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    run_id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    product_id: Mapped[str] = mapped_column(String, nullable=False)
    signal_id: Mapped[str] = mapped_column(String, ForeignKey("signals.signal_id"), nullable=False)
    # Groups the runs created from one signal fan-out (US-49). NULL for legacy
    # single runs — treated as a batch of one (no portfolio synthesis).
    batch_id: Mapped[str | None] = mapped_column(String, nullable=True)
    mode: Mapped[RunMode | None] = mapped_column(SAEnum(RunMode), nullable=True)
    recommendation_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # S2 suggested_mode + reasoning
    routing: Mapped[Routing | None] = mapped_column(SAEnum(Routing), nullable=True)
    composite_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now)
    # Bumped on every change (US: gate-watcher dedup needs to detect a run
    # re-entering a gate state, e.g. waiting_approval after a Gate 2 revise).
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utc_now, onupdate=_utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Per-run LLM token totals (Phase 2), summed across all stage outputs at
    # finalize. NULL for runs that predate this column or never called an LLM.
    prompt_tokens_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens_total: Mapped[int | None] = mapped_column(Integer, nullable=True)

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
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    root_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # How this run was started. "start" = normal Gate 0 / manual / fan-out start
    # (the default). "refresh" = started by POST /signals/{id}/refresh after the
    # signal's content was re-ingested. A refresh begins a FRESH attempt lineage
    # (attempt_no resets to 1, root_run_id NULL) so the observatory shows it as a
    # re-ingest, not as "attempt N of N" of a failure-retry lineage. create_run
    # scopes attempt counting to runs at/after the latest refresh boundary.
    origin: Mapped[str] = mapped_column(String, nullable=False, default="start")
    # The caller's telemetry session, supplied by whoever POSTed /runs/start
    # (telemetry plan P3). The engine never generates it and never interprets it
    # — it is an opaque key that the run's own spans carry as
    # `langfuse.session.id`, so the run's trace and the agent turn that asked for
    # it land under one session instead of being two unrelated traces.
    # NULL = the caller was not being traced, which must stay distinguishable
    # from "" (see SQLiteStore.create_run).
    origin_trace_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # New evaluation behavior is versioned per run. Existing rows and callers
    # remain on legacy until evidence_v1 is explicitly selected and validated.
    decision_pipeline_version: Mapped[str] = mapped_column(
        String, nullable=False, default="legacy"
    )
    # The legacy failed_stage / error / ended_by diagnostic columns were dropped in
    # US-55 step 7d-3 — their information lives in the canonical columns below: a
    # failed run's stage → position, its error → reason; a killed run's stop-kind →
    # reason. (SQLiteStore._migrate drops the physical columns.)

    # --- Canonical (position, lifecycle) columns (US-55) ---
    # The redesign's target vocabulary, PHYSICALLY stored and AUTHORITATIVE
    # (US-55 step 7d-1). The store writes them directly via advance/pause/finish;
    # nothing reads or writes the legacy status/current_stage columns anymore
    # (those are dropped in step 7d-2). The direct-SQLite reader (observatory) and
    # ops plane read these columns.
    #   lifecycle: running | paused | done
    #   position:  furthest stage reached (s1..s7) — NOT cleared on finalize
    #   outcome:   completed | stopped | failed  (NULL while live)
    #   reason:    why it stopped / the error    (NULL otherwise)
    lifecycle: Mapped[str | None] = mapped_column(String, nullable=True)
    position: Mapped[str | None] = mapped_column(String, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String, nullable=True)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)

    signal: Mapped["Signal"] = relationship(back_populates="runs")
    stage_outputs: Mapped[list["StageOutput"]] = relationship(back_populates="run")
    approval_events: Mapped[list["ApprovalEvent"]] = relationship(back_populates="run")
    artifacts: Mapped[list["Artifact"]] = relationship(back_populates="run")


class DecisionCaseRecord(Base):
    """Immutable case payload; revisions are pinned by ``DecisionCaseRunLink``."""

    __tablename__ = "decision_cases"

    case_id: Mapped[str] = mapped_column(String, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    prepared_context_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now)


class DecisionCaseRunLink(Base):
    """One immutable DecisionCase revision for each evidence-aware run."""

    __tablename__ = "decision_case_run_links"

    run_id: Mapped[str] = mapped_column(
        String, ForeignKey("workflow_runs.run_id"), primary_key=True
    )
    case_id: Mapped[str] = mapped_column(String, nullable=False)
    case_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now)


class DecisionRequestRecord(Base):
    """Durable idempotency result for the workflow-side decision bridge."""

    __tablename__ = "decision_requests"

    actor: Mapped[str] = mapped_column(String, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String, primary_key=True)
    request_hash: Mapped[str] = mapped_column(String, nullable=False)
    request_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, default=_new_uuid)
    signal_id: Mapped[str] = mapped_column(String, nullable=False)
    run_id: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now)


class StageOutput(Base):
    __tablename__ = "stage_outputs"

    output_id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    run_id: Mapped[str] = mapped_column(String, ForeignKey("workflow_runs.run_id"), nullable=False)
    stage: Mapped[str] = mapped_column(String, nullable=False)
    output_json: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now)

    run: Mapped["WorkflowRun"] = relationship(back_populates="stage_outputs")


class ApprovalEvent(Base):
    __tablename__ = "approval_events"

    event_id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    run_id: Mapped[str] = mapped_column(String, ForeignKey("workflow_runs.run_id"), nullable=False)
    stage: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[ApprovalAction] = mapped_column(SAEnum(ApprovalAction), nullable=False)
    feedback_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now)

    run: Mapped["WorkflowRun"] = relationship(back_populates="approval_events")


class Artifact(Base):
    __tablename__ = "artifacts"

    artifact_id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    run_id: Mapped[str] = mapped_column(String, ForeignKey("workflow_runs.run_id"), nullable=False)
    type: Mapped[ArtifactType] = mapped_column(SAEnum(ArtifactType), nullable=False)
    content_md: Mapped[str] = mapped_column(Text, nullable=False)
    source_stage: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now)

    run: Mapped["WorkflowRun"] = relationship(back_populates="artifacts")


class RunBatch(Base):
    """A fan-out batch: the set of runs created from one signal (US-49).

    `membership_closed` guards the portfolio-synthesis trigger. Membership is
    dynamic — the manual Portfolio Scan gate can add runs after the first run
    has started — so synthesis must not fire while more runs might still join.
    """

    __tablename__ = "run_batches"

    batch_id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    signal_id: Mapped[str] = mapped_column(String, ForeignKey("signals.signal_id"), nullable=False)
    membership_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now)
    # The full Portfolio Triage verdict for this batch: every product's
    # relevance_score and reason, as returned by the single triage call, as JSON.
    #
    # It was computed and thrown away. The response carried it to the caller and
    # nothing stored it, so afterwards there was no way to tell a routing decision
    # that was a coin flip from one that was settled — and a mis-routed run cannot
    # be judged at Gate 1 without that. Nullable: batches created before this
    # column stay NULL, which reads correctly as "not recorded".
    triage_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class PortfolioSynthesis(Base):
    """Post-hoc cross-product memo for one fan-out batch (US-49, Variant 2).

    One synthesis per batch (batch_id is the PK — the insert-or-skip idempotency
    guard). Batch/signal-scoped, so deliberately not an Artifact row (which is
    run-scoped).
    """

    __tablename__ = "portfolio_syntheses"

    batch_id: Mapped[str] = mapped_column(String, primary_key=True)
    signal_id: Mapped[str] = mapped_column(String, ForeignKey("signals.signal_id"), nullable=False)
    content_md: Mapped[str] = mapped_column(Text, nullable=False)
    content_json: Mapped[str] = mapped_column(Text, nullable=False)
    run_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now)
