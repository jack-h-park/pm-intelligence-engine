import json
from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.models.workflow import (
    Artifact,
    ApprovalAction,
    ApprovalEvent,
    ArtifactType,
    Base,
    DecisionCaseRecord,
    DecisionCaseRunLink,
    DecisionRequestRecord,
    PortfolioSynthesis,
    Routing,
    RunBatch,
    RunMode,
    Signal,
    SignalCategory,
    SignalNote,
    SignalStatus,
    SignalTag,
    SourceType,
    StageOutput,
    WorkflowRun,
)
from app.services.signal_tags import (
    MAX_TAGS_PER_SIGNAL,
    TagError,
    normalize_tag,
    normalize_tags,
)



def _loads_or_none(raw):
    """Parse stored JSON, or None. A malformed blob reads as absent rather than
    raising: the caller's fallback is "not recorded", which is safe, whereas a
    500 on a batch read would take a gate message down with it."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


class SQLiteStore:
    def __init__(self, database_url: str) -> None:
        self._engine = create_engine(database_url, connect_args={"check_same_thread": False})
        Base.metadata.create_all(self._engine)
        self._migrate_schema()
        self._Session = sessionmaker(bind=self._engine)

    def _migrate_schema(self) -> None:
        """Idempotent in-place migration for existing local DBs (no Alembic).

        ``create_all`` creates new tables but never alters existing ones, so
        columns added to a model are missing from a pre-existing SQLite file.
        Add them here, guarded by a column-existence check. New tables
        (run_batches, portfolio_syntheses, signal_notes, signal_tags) are handled
        by ``create_all``.
        """
        inspector = inspect(self._engine)
        run_columns = {c["name"] for c in inspector.get_columns("workflow_runs")}
        # Portfolio Triage's per-product scores. Nullable ADD COLUMN — batches
        # created before it stay NULL, which reads correctly as "not recorded"
        # rather than "no other product was relevant". Guarded, so idempotent.
        if inspector.has_table("run_batches"):
            batch_columns = {c["name"] for c in inspector.get_columns("run_batches")}
            if "triage_json" not in batch_columns:
                with self._engine.begin() as conn:
                    conn.execute(text("ALTER TABLE run_batches ADD COLUMN triage_json TEXT"))
        if "batch_id" not in run_columns:
            with self._engine.begin() as conn:
                conn.execute(text("ALTER TABLE workflow_runs ADD COLUMN batch_id VARCHAR"))
        # Phase 2: per-run LLM token totals. Nullable ADD COLUMN — existing runs
        # stay NULL; no rewrite, safe on the always-on DB.
        if "prompt_tokens_total" not in run_columns:
            with self._engine.begin() as conn:
                conn.execute(text("ALTER TABLE workflow_runs ADD COLUMN prompt_tokens_total INTEGER"))
                conn.execute(text("ALTER TABLE workflow_runs ADD COLUMN completion_tokens_total INTEGER"))

        # Retry lineage & failure diagnostics. Nullable/defaulted ADD COLUMNs —
        # existing runs become attempt 1 with no lineage parent and no error,
        # which is the correct interpretation for pre-migration rows. Guarded →
        # idempotent. attempt_no carries a DEFAULT so the NOT NULL model column
        # is satisfied for existing rows.
        if "attempt_no" not in run_columns:
            with self._engine.begin() as conn:
                conn.execute(text("ALTER TABLE workflow_runs ADD COLUMN attempt_no INTEGER NOT NULL DEFAULT 1"))
                conn.execute(text("ALTER TABLE workflow_runs ADD COLUMN root_run_id VARCHAR"))
                conn.execute(text("ALTER TABLE workflow_runs ADD COLUMN failed_stage VARCHAR"))
                conn.execute(text("ALTER TABLE workflow_runs ADD COLUMN error TEXT"))
                # One-time backfill so the fix is retroactive: reconstruct the
                # lineage of pre-existing runs (numbered per signal+product by
                # creation order) instead of leaving every historical run as a
                # lone attempt 1. Without this, past retries stay scattered.
                # Window functions require SQLite >= 3.25 (modern Python and
                # better-sqlite3 both ship newer).
                conn.execute(
                    text(
                        """
                        WITH ranked AS (
                            SELECT run_id,
                                   ROW_NUMBER() OVER w AS rn,
                                   FIRST_VALUE(run_id) OVER w AS root
                            FROM workflow_runs
                            WINDOW w AS (
                                PARTITION BY signal_id, product_id
                                ORDER BY created_at ASC, run_id ASC
                            )
                        )
                        UPDATE workflow_runs
                        SET attempt_no = (SELECT rn FROM ranked WHERE ranked.run_id = workflow_runs.run_id),
                            root_run_id = (
                                SELECT CASE WHEN rn = 1 THEN NULL ELSE root END
                                FROM ranked WHERE ranked.run_id = workflow_runs.run_id
                            )
                        """
                    )
                )

        # Provenance back-link to the originating sensing file (nullable). A plain
        # ADD COLUMN suffices since it carries no constraint. Guarded → idempotent.
        if "source_ref" not in {c["name"] for c in inspector.get_columns("signals")}:
            with self._engine.begin() as conn:
                conn.execute(text("ALTER TABLE signals ADD COLUMN source_ref VARCHAR"))
            inspector = inspect(self._engine)  # refresh before the US-49 check below

        # US-49 renamed signals.product_id (NOT NULL) -> original_product_id
        # (nullable). A plain ADD COLUMN can't express either change, and SQLite's
        # RENAME COLUMN keeps the NOT NULL constraint, so rebuild the table to
        # match the model. Guarded by the column check → idempotent.
        signal_columns = {c["name"] for c in inspector.get_columns("signals")}
        if "original_product_id" not in signal_columns and "product_id" in signal_columns:
            with self._engine.begin() as conn:
                conn.execute(text("ALTER TABLE signals RENAME TO signals__legacy_us49"))
            Signal.__table__.create(self._engine)
            with self._engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO signals (signal_id, original_product_id, title, "
                        "source_url, raw_content, category, status, source_type, ingested_at) "
                        "SELECT signal_id, product_id, title, source_url, raw_content, "
                        "category, status, source_type, ingested_at FROM signals__legacy_us49"
                    )
                )
                conn.execute(text("DROP TABLE signals__legacy_us49"))

        # State-vocabulary renames (state glossary, 2026-06-14). Idempotent —
        # the WHERE clause matches only legacy values, so re-running is a no-op.
        # The enum `_missing_` hook accepts the legacy strings in-code; this UPDATE
        # migrates the stored rows so SQLAlchemy reads resolve directly.
        # (The workflow_runs.status rename is gone — the engine no longer reads or
        # writes that column, and step 7d-2 drops it. US-55.)
        with self._engine.begin() as conn:
            conn.execute(
                text("UPDATE signals SET status = 'new' WHERE status = 'pending'")
            )

        # Signal re-ingest support (POST /signals/{id}/refresh). Plain ADD COLUMNs,
        # both nullable / defaulted, so guarded checks keep them idempotent.
        inspector = inspect(self._engine)
        if "refreshed_at" not in {c["name"] for c in inspector.get_columns("signals")}:
            with self._engine.begin() as conn:
                conn.execute(text("ALTER TABLE signals ADD COLUMN refreshed_at DATETIME"))
        if "origin" not in {c["name"] for c in inspector.get_columns("workflow_runs")}:
            with self._engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE workflow_runs "
                        "ADD COLUMN origin VARCHAR NOT NULL DEFAULT 'start'"
                    )
                )

        # (The legacy `ended_by` column was added here in US-52 and dropped in
        # US-55 step 7d-3 — its terminal-reason now lives in `reason`.)

        # Canonical (position, lifecycle) columns (US-55 step 6). Plain nullable
        # ADD COLUMNs. These are now the authoritative run-state columns (US-55
        # step 7d-1); the store writes them directly via advance/pause/finish.
        # Guarded → idempotent. Rows in prod were backfilled in step 7c.
        run_cols = {c["name"] for c in inspector.get_columns("workflow_runs")}
        for col in ("lifecycle", "position", "outcome", "reason"):
            if col not in run_cols:
                with self._engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE workflow_runs ADD COLUMN {col} VARCHAR"))

        # US-55 step 7d-2/7d-3: drop the legacy state + diagnostic columns. Nothing
        # reads or writes them anymore — the canonical (lifecycle, position, outcome,
        # reason) columns are authoritative (a failed run's stage/error and a killed
        # run's stop-kind live in position/reason). Guarded → idempotent. SQLite DROP
        # COLUMN needs >= 3.35 (prod is 3.37); if unsupported, no-op and leave vestigial.
        run_cols_after = {c["name"] for c in inspect(self._engine).get_columns("workflow_runs")}
        for col in ("status", "current_stage", "ended_by", "failed_stage", "error"):
            if col in run_cols_after:
                try:
                    with self._engine.begin() as conn:
                        conn.execute(text(f"ALTER TABLE workflow_runs DROP COLUMN {col}"))
                except Exception:  # noqa: BLE001 — pre-3.35 SQLite: leave column vestigial
                    pass

        # Drop artifacts.content_json. Every stage wrote it as the same
        # `output.model_dump_json()` string it had just saved to stage_outputs, so
        # the column was a verbatim second copy of the canonical row — measured on
        # the live DB, all 252 artifact rows matched their stage output byte for
        # byte. Nothing ever read it back: not the pipeline (which loads
        # stage_outputs), not the dashboard, not the ops plane. Same guarded, idempotent
        # DROP COLUMN as the sweep above.
        artifact_cols = {c["name"] for c in inspect(self._engine).get_columns("artifacts")}
        if "content_json" in artifact_cols:
            try:
                with self._engine.begin() as conn:
                    conn.execute(text("ALTER TABLE artifacts DROP COLUMN content_json"))
            except Exception:  # noqa: BLE001 — pre-3.35 SQLite: leave column vestigial
                pass

    # --- Signal ---

    def save_signal(
        self,
        title: str,
        raw_content: str,
        original_product_id: Optional[str] = None,
        source_url: Optional[str] = None,
        category: str = "other",
        source_type: str = "manual",
        source_ref: Optional[str] = None,
    ) -> str:
        with self._Session() as session:
            signal = Signal(
                original_product_id=original_product_id,
                title=title,
                raw_content=raw_content,
                source_url=source_url,
                category=SignalCategory(category),
                source_type=SourceType(source_type),
                source_ref=source_ref,
            )
            session.add(signal)
            session.commit()
            return signal.signal_id

    def get_signal(self, signal_id: str) -> Optional[dict]:
        with self._Session() as session:
            s = session.get(Signal, signal_id)
            return self._signal_to_dict(s) if s else None

    def update_signal_status(self, signal_id: str, status: str) -> None:
        with self._Session() as session:
            s = session.get(Signal, signal_id)
            if s:
                s.status = SignalStatus(status)
                session.commit()

    def update_signal_content(
        self,
        signal_id: str,
        raw_content: str,
        category: Optional[str] = None,
    ) -> Optional[dict]:
        """Re-ingest a signal's content (POST /signals/{id}/refresh).

        Signals are otherwise immutable after Gate 0 intake; this is the single
        audited path that overwrites ``raw_content`` — used when the original
        crawl captured site-chrome / a bot-wall page and a better fetch recovered
        the article. Stamps ``refreshed_at`` and, when given, re-infers category.
        Returns the updated signal dict, or None if the signal does not exist.
        """
        with self._Session() as session:
            s = session.get(Signal, signal_id)
            if s is None:
                return None
            s.raw_content = raw_content
            if category is not None:
                s.category = SignalCategory(category)
            s.refreshed_at = datetime.now(UTC)
            session.commit()
            return self._signal_to_dict(s)

    def list_signals(
        self,
        original_product_id: Optional[str] = None,
        status: Optional[str] = None,
        tag: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        with self._Session() as session:
            q = session.query(Signal)
            if original_product_id:
                q = q.filter(Signal.original_product_id == original_product_id)
            if status:
                q = q.filter(Signal.status == SignalStatus(status))
            if tag:
                # Normalised at the boundary so a caller can pass the tag as typed.
                q = q.filter(
                    Signal.signal_id.in_(
                        session.query(SignalTag.signal_id).filter(
                            SignalTag.tag == normalize_tag(tag)
                        )
                    )
                )
            q = q.order_by(Signal.ingested_at.desc()).limit(limit)
            return [self._signal_to_dict(s) for s in q.all()]

    # --- Signal notes (append-only) & tags (mutable set) ---

    def add_signal_note(
        self,
        signal_id: str,
        body: str,
        author: str,
        context: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Append a review note. Returns None if the signal does not exist.

        There is deliberately no update/delete counterpart — see ``SignalNote``.
        """
        with self._Session() as session:
            if session.get(Signal, signal_id) is None:
                return None
            note = SignalNote(
                signal_id=signal_id,
                body=body,
                author=author,
                context=context,
                run_id=run_id,
            )
            session.add(note)
            session.commit()
            return self._signal_note_to_dict(note)

    def list_signal_notes(
        self, signal_id: str, include_superseded: bool = False
    ) -> list[dict]:
        """Notes oldest-first — the order the judgments were actually made in."""
        with self._Session() as session:
            q = session.query(SignalNote).filter(SignalNote.signal_id == signal_id)
            if not include_superseded:
                q = q.filter(SignalNote.superseded_by.is_(None))
            q = q.order_by(SignalNote.created_at.asc(), SignalNote.note_id.asc())
            return [self._signal_note_to_dict(n) for n in q.all()]

    def add_signal_tags(
        self, signal_id: str, tags: list[str], author: str
    ) -> Optional[list[str]]:
        """Add tags (idempotent). Returns the signal's full tag set, or None if
        the signal does not exist.

        Re-adding an existing tag is a no-op that keeps the original author and
        timestamp — the first person to apply a label is the one who judged it.
        """
        with self._Session() as session:
            if session.get(Signal, signal_id) is None:
                return None
            existing = {
                t.tag
                for t in session.query(SignalTag).filter(
                    SignalTag.signal_id == signal_id
                )
            }
            for tag in normalize_tags(tags):
                if tag in existing:
                    continue
                if len(existing) >= MAX_TAGS_PER_SIGNAL:
                    raise TagError(
                        f"signal already carries {MAX_TAGS_PER_SIGNAL} tags "
                        "— remove one before adding another"
                    )
                session.add(
                    SignalTag(signal_id=signal_id, tag=tag, author=author)
                )
                existing.add(tag)
            session.commit()
            return sorted(existing)

    def remove_signal_tags(
        self, signal_id: str, tags: list[str]
    ) -> Optional[list[str]]:
        """Remove tags (idempotent). Returns the remaining set, or None if the
        signal does not exist. Removing an absent tag is not an error."""
        with self._Session() as session:
            if session.get(Signal, signal_id) is None:
                return None
            for tag in normalize_tags(tags):
                session.query(SignalTag).filter(
                    SignalTag.signal_id == signal_id, SignalTag.tag == tag
                ).delete()
            session.commit()
            return sorted(
                t.tag
                for t in session.query(SignalTag).filter(
                    SignalTag.signal_id == signal_id
                )
            )

    # --- WorkflowRun ---

    @staticmethod
    def _create_decision_request_run(session, case) -> dict:
        signal = Signal(
            original_product_id=case.product_id,
            title="Direct product decision input",
            raw_content=case.decision_question,
            category=SignalCategory.other,
            source_type=SourceType.manual,
            source_ref=f"decision-case:{case.case_id}:{case.revision}",
        )
        session.add(signal)
        session.flush()
        run = WorkflowRun(
            product_id=case.product_id,
            signal_id=signal.signal_id,
            attempt_no=1,
            origin="decision_request",
            lifecycle="running",
            position=None,
            outcome=None,
            reason=None,
        )
        session.add(run)
        session.flush()
        signal.status = SignalStatus.in_run
        session.add(
            DecisionCaseRecord(
                case_id=case.case_id,
                revision=case.revision,
                product_id=case.product_id,
                prepared_context_id=case.prepared_context_id,
                payload_json=case.model_dump_json(),
            )
        )
        session.add(
            DecisionCaseRunLink(
                run_id=run.run_id, case_id=case.case_id, case_revision=case.revision
            )
        )
        return {"signal_id": signal.signal_id, "run_id": run.run_id}

    def create_decision_request_run(self, case) -> dict:
        """Create the legacy-compatible signal, run, and case link atomically.

        The signal is deliberately typed as a direct decision input through its
        ``source_ref``.  It is not synthetic external news, and this method
        creates no portfolio siblings.
        """
        from app.models.decision_case import DecisionCase

        if not isinstance(case, DecisionCase):
            raise TypeError("case must be a DecisionCase")
        with self._Session.begin() as session:
            return self._create_decision_request_run(session, case)

    def create_idempotent_decision_request(
        self, actor: str, idempotency_key: str, request_hash: str, case
    ) -> tuple[dict, int]:
        """Atomically create or replay the one workflow-side request result."""
        from app.models.decision_case import DecisionCase

        if not isinstance(case, DecisionCase):
            raise TypeError("case must be a DecisionCase")
        with self._Session.begin() as session:
            existing = session.get(
                DecisionRequestRecord,
                {"actor": actor, "idempotency_key": idempotency_key},
            )
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise ValueError("Idempotency-Key was already used with different content")
                return {
                    "request_id": existing.request_id,
                    "signal_id": existing.signal_id,
                    "run_id": existing.run_id,
                }, 200
            result = self._create_decision_request_run(session, case)
            request = DecisionRequestRecord(
                actor=actor,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                signal_id=result["signal_id"],
                run_id=result["run_id"],
            )
            session.add(request)
            session.flush()
            return {"request_id": request.request_id, **result}, 202

    def save_decision_case(self, run_id: str, case) -> None:
        """Persist a case revision and its run link in one transaction."""
        from app.models.decision_case import DecisionCase

        if not isinstance(case, DecisionCase):
            raise TypeError("case must be a DecisionCase")
        with self._Session.begin() as session:
            if session.get(WorkflowRun, run_id) is None:
                raise ValueError(f"Run {run_id} not found")
            existing = session.get(
                DecisionCaseRecord, {"case_id": case.case_id, "revision": case.revision}
            )
            payload_json = case.model_dump_json()
            if existing is None:
                session.add(
                    DecisionCaseRecord(
                        case_id=case.case_id,
                        revision=case.revision,
                        product_id=case.product_id,
                        prepared_context_id=case.prepared_context_id,
                        payload_json=payload_json,
                    )
                )
            elif existing.payload_json != payload_json:
                raise ValueError("DecisionCase revisions are immutable")
            link = session.get(DecisionCaseRunLink, run_id)
            if link is None:
                session.add(
                    DecisionCaseRunLink(
                        run_id=run_id, case_id=case.case_id, case_revision=case.revision
                    )
                )
            elif (link.case_id, link.case_revision) != (case.case_id, case.revision):
                raise ValueError("Run already has a different DecisionCase revision")

    def get_decision_case(self, run_id: str):
        """Return the exact case revision pinned to a run, if it has one."""
        from app.models.decision_case import DecisionCase

        with self._Session() as session:
            link = session.get(DecisionCaseRunLink, run_id)
            if link is None:
                return None
            record = session.get(
                DecisionCaseRecord,
                {"case_id": link.case_id, "revision": link.case_revision},
            )
            return DecisionCase.model_validate_json(record.payload_json) if record else None

    def create_run(
        self,
        product_id: str,
        signal_id: str,
        batch_id: Optional[str] = None,
        origin: str = "start",
    ) -> str:
        with self._Session() as session:
            # Derive retry lineage from prior runs of the same (signal_id,
            # product_id). A fan-out spawns one run per *product* from a single
            # signal — those are independent lineages (attempt 1 each), so the
            # product_id is part of the key. A failure-retry re-runs the same
            # signal+product, which is what increments attempt_no here.
            prior = (
                session.query(WorkflowRun)
                .filter(
                    WorkflowRun.signal_id == signal_id,
                    WorkflowRun.product_id == product_id,
                )
                .order_by(WorkflowRun.created_at.asc())
                .all()
            )
            # A refresh (re-ingest of content) begins a FRESH attempt lineage so
            # it is not rendered as "attempt N of N" of the prior failure-retry
            # lineage. It also acts as a boundary: a later failure-retry counts
            # only runs at/after the most recent refresh, not the whole history.
            if origin == "refresh":
                scoped: list[WorkflowRun] = []
            else:
                last_refresh = next(
                    (i for i in range(len(prior) - 1, -1, -1) if prior[i].origin == "refresh"),
                    None,
                )
                scoped = prior if last_refresh is None else prior[last_refresh:]
            attempt_no = len(scoped) + 1
            root_run_id = None
            if scoped:
                first = scoped[0]
                # COALESCE semantics: attempt 1's root_run_id is NULL (it is the
                # root), so fall back to its own run_id for later attempts.
                root_run_id = first.root_run_id or first.run_id
            run = WorkflowRun(
                product_id=product_id,
                signal_id=signal_id,
                batch_id=batch_id,
                attempt_no=attempt_no,
                root_run_id=root_run_id,
                origin=origin,
                # Authoritative initial state (US-55 step 7d-1): a fresh run is live
                # with no position yet. advance("s1") sets the first position.
                lifecycle="running",
                position=None,
                outcome=None,
                reason=None,
            )
            session.add(run)
            session.commit()
            return run.run_id

    def get_run(self, run_id: str) -> Optional[dict]:
        with self._Session() as session:
            r = session.get(WorkflowRun, run_id)
            return self._run_to_dict(r) if r else None

    def update_run(self, run_id: str, **kwargs) -> None:
        """Set non-state fields on a run (mode/routing/tokens/recommendation/…).

        Run STATE (lifecycle/position/outcome/reason + completed_at) is written
        only via advance/pause/finish (US-55 step 7d-1). This method no longer
        accepts ``status``/``current_stage``.
        """
        with self._Session() as session:
            r = session.get(WorkflowRun, run_id)
            if not r:
                return
            for key, value in kwargs.items():
                if key == "routing" and value is not None:
                    value = Routing(value)
                elif key == "mode" and value is not None:
                    value = RunMode(value)
                setattr(r, key, value)
            session.commit()

    def advance(self, run_id: str, position: str, **extra) -> None:
        """Move a run to *running* at ``position`` (US-55).

        ``(lifecycle, position)`` is the authoritative run-state write. ``extra``
        passes through non-state fields (e.g. ``mode``).
        """
        self._set_live_state(run_id, "running", position, extra)

    def pause(self, run_id: str, position: str, **extra) -> None:
        """Pause a run at the gate at ``position`` (s2/s4/s5) — see :meth:`advance`."""
        self._set_live_state(run_id, "paused", position, extra)

    def _set_live_state(self, run_id: str, lifecycle: str, position: str, extra: dict) -> None:
        with self._Session() as session:
            r = session.get(WorkflowRun, run_id)
            if not r:
                return
            # Authoritative (position, lifecycle) write.
            r.lifecycle = lifecycle
            r.position = position
            r.outcome = None
            r.reason = None
            for key, value in extra.items():
                if key == "mode" and value is not None:
                    value = RunMode(value)
                elif key == "routing" and value is not None:
                    value = Routing(value)
                setattr(r, key, value)
            session.commit()

    def finish(
        self,
        run_id: str,
        outcome: str,
        position: str | None = None,
        reason: str | None = None,
        **extra,
    ) -> None:
        """Apply a terminal state (US-55 step 7b-2).

        ``(lifecycle=done, position, outcome, reason)`` is the authoritative write.
        ``completed_at`` is stamped for ``completed``/``stopped`` outcomes (not
        ``failed``). ``extra`` carries the diagnostic detail the finalizer supplies
        (``ended_by``/``failed_stage``/``error``) plus token totals.
        """
        with self._Session() as session:
            r = session.get(WorkflowRun, run_id)
            if not r:
                return
            # Authoritative terminal write.
            r.lifecycle = "done"
            r.position = position
            r.outcome = outcome
            r.reason = reason
            if outcome in ("completed", "stopped") and r.completed_at is None:
                r.completed_at = datetime.now(UTC)
            for key, value in extra.items():
                setattr(r, key, value)
            session.commit()

    def list_runs(
        self,
        product_id: Optional[str] = None,
        routing: Optional[str] = None,
        event: Optional[str] = None,
        since: Optional[datetime] = None,
        batch_id: Optional[str] = None,
        signal_id: Optional[str] = None,
        lifecycle: Optional[str] = None,
        position: Optional[str] = None,
        outcome: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        with self._Session() as session:
            q = session.query(WorkflowRun)
            if product_id:
                q = q.filter(WorkflowRun.product_id == product_id)
            # US-55: filter on the canonical (lifecycle, position, outcome) columns.
            # `outcome` distinguishes the three terminal states (completed / stopped
            # / failed) — without it, ?lifecycle=done returns all terminals.
            if lifecycle:
                q = q.filter(WorkflowRun.lifecycle == lifecycle)
            if position:
                q = q.filter(WorkflowRun.position == position)
            if outcome:
                q = q.filter(WorkflowRun.outcome == outcome)
            if routing:
                q = q.filter(WorkflowRun.routing == Routing(routing))
            if batch_id:
                q = q.filter(WorkflowRun.batch_id == batch_id)
            if signal_id:
                q = q.filter(WorkflowRun.signal_id == signal_id)
            if event:
                q = q.join(ApprovalEvent).filter(
                    ApprovalEvent.action == ApprovalAction(event)
                )
            if since:
                q = q.filter(WorkflowRun.created_at >= since)
            q = q.order_by(WorkflowRun.created_at.desc()).limit(limit)
            return [self._run_to_dict(r) for r in q.all()]

    # --- StageOutput ---

    def save_stage_output(
        self,
        run_id: str,
        stage: str,
        output_json: str,
        version: int = 1,
    ) -> str:
        with self._Session() as session:
            so = StageOutput(run_id=run_id, stage=stage, output_json=output_json, version=version)
            session.add(so)
            session.commit()
            return so.output_id

    def get_stage_output(
        self,
        run_id: str,
        stage: str,
        version: Optional[int] = None,
    ) -> Optional[dict]:
        with self._Session() as session:
            q = session.query(StageOutput).filter(
                StageOutput.run_id == run_id,
                StageOutput.stage == stage,
            )
            if version is not None:
                q = q.filter(StageOutput.version == version)
            else:
                q = q.order_by(StageOutput.version.desc())
            so = q.first()
            return self._stage_output_to_dict(so) if so else None

    def get_all_stage_outputs(self, run_id: str) -> list[dict]:
        with self._Session() as session:
            outputs = (
                session.query(StageOutput)
                .filter(StageOutput.run_id == run_id)
                .order_by(StageOutput.stage, StageOutput.version)
                .all()
            )
            return [self._stage_output_to_dict(so) for so in outputs]

    # --- ApprovalEvent ---

    def record_approval(
        self,
        run_id: str,
        stage: str,
        action: str,
        feedback_text: Optional[str] = None,
    ) -> str:
        with self._Session() as session:
            event = ApprovalEvent(
                run_id=run_id,
                stage=stage,
                action=ApprovalAction(action),
                feedback_text=feedback_text,
            )
            session.add(event)
            session.commit()
            return event.event_id

    def get_approval_events(self, run_id: str) -> list[dict]:
        with self._Session() as session:
            events = (
                session.query(ApprovalEvent)
                .filter(ApprovalEvent.run_id == run_id)
                .order_by(ApprovalEvent.created_at.asc())
                .all()
            )
            return [
                {
                    "event_id": e.event_id,
                    "run_id": e.run_id,
                    "stage": e.stage,
                    "action": e.action.value,
                    "feedback_text": e.feedback_text,
                    "created_at": e.created_at.isoformat() if e.created_at else None,
                }
                for e in events
            ]

    # --- Artifact ---

    def save_artifact(
        self,
        run_id: str,
        artifact_type: str,
        content_md: str,
        source_stage: Optional[str] = None,
    ) -> str:
        with self._Session() as session:
            artifact = Artifact(
                run_id=run_id,
                type=ArtifactType(artifact_type),
                content_md=content_md,
                source_stage=source_stage,
            )
            session.add(artifact)
            session.commit()
            return artifact.artifact_id

    def list_artifacts(
        self,
        run_id: str,
        artifact_type: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict]:
        with self._Session() as session:
            q = session.query(Artifact).filter(Artifact.run_id == run_id)
            if artifact_type:
                q = q.filter(Artifact.type == ArtifactType(artifact_type))
            q = q.order_by(Artifact.created_at.desc()).limit(limit)
            return [self._artifact_to_dict(a) for a in q.all()]

    # --- RunBatch (US-49) ---

    def create_batch(self, signal_id: str, triage: Optional[list] = None) -> str:
        """Create a fan-out batch, recording Triage's verdict when one is given.

        ``triage`` is the full per-product score list. It is stored at creation
        because that is the only moment it exists — the call that produces it is
        not repeated, and reconstructing it later would mean re-scoring against
        profiles that may since have changed.
        """
        with self._Session() as session:
            batch = RunBatch(
                signal_id=signal_id,
                triage_json=json.dumps(triage, ensure_ascii=False) if triage else None,
            )
            session.add(batch)
            session.commit()
            return batch.batch_id

    def get_batch(self, batch_id: str) -> Optional[dict]:
        with self._Session() as session:
            b = session.get(RunBatch, batch_id)
            if b is None:
                return None
            return {
                "batch_id": b.batch_id,
                "signal_id": b.signal_id,
                "membership_closed": b.membership_closed,
                "created_at": b.created_at.isoformat(),
                # None when the batch predates the column, or when triage was not
                # recorded. A caller must not read that as "nothing else scored".
                "triage": _loads_or_none(b.triage_json),
            }

    def close_batch_membership(self, batch_id: str) -> None:
        """Mark a batch's membership final so the synthesis trigger may fire."""
        with self._Session() as session:
            b = session.get(RunBatch, batch_id)
            if b:
                b.membership_closed = True
                session.commit()

    # --- PortfolioSynthesis (US-49, Variant 2) ---

    def save_portfolio_synthesis(
        self,
        batch_id: str,
        signal_id: str,
        content_md: str,
        content_json: str,
        run_ids_json: str,
    ) -> bool:
        """Insert one synthesis per batch; skip if one already exists.

        Returns True if inserted, False if a row was already present — the
        insert-or-skip idempotency guard for near-simultaneous batch completion.
        """
        with self._Session() as session:
            if session.get(PortfolioSynthesis, batch_id) is not None:
                return False
            session.add(
                PortfolioSynthesis(
                    batch_id=batch_id,
                    signal_id=signal_id,
                    content_md=content_md,
                    content_json=content_json,
                    run_ids_json=run_ids_json,
                )
            )
            session.commit()
            return True

    def get_portfolio_synthesis(self, batch_id: str) -> Optional[dict]:
        with self._Session() as session:
            p = session.get(PortfolioSynthesis, batch_id)
            if p is None:
                return None
            return {
                "batch_id": p.batch_id,
                "signal_id": p.signal_id,
                "content_md": p.content_md,
                "content_json": p.content_json,
                "run_ids_json": p.run_ids_json,
                "created_at": p.created_at.isoformat(),
            }

    # --- Serializers ---

    @staticmethod
    def _signal_to_dict(s: Signal) -> dict:
        return {
            "signal_id": s.signal_id,
            "original_product_id": s.original_product_id,
            "title": s.title,
            "source_url": s.source_url,
            "source_ref": s.source_ref,
            "raw_content": s.raw_content,
            "category": s.category.value,
            "status": s.status.value,
            "source_type": s.source_type.value,
            "ingested_at": s.ingested_at.isoformat(),
            "refreshed_at": s.refreshed_at.isoformat() if s.refreshed_at else None,
            # Labels ride along on every signal read — they are the cheap facet
            # consumers filter on. Note *bodies* do not: they are unbounded text,
            # so the list endpoint carries only the count and GET
            # /signals/{id}/notes serves the contents (same reasoning that keeps
            # raw_content off the list response).
            "tags": sorted(t.tag for t in s.tags),
            "note_count": sum(1 for n in s.notes if n.superseded_by is None),
        }

    @staticmethod
    def _signal_note_to_dict(n: SignalNote) -> dict:
        return {
            "note_id": n.note_id,
            "signal_id": n.signal_id,
            "body": n.body,
            "author": n.author,
            "context": n.context,
            "run_id": n.run_id,
            "created_at": n.created_at.isoformat(),
            "superseded_by": n.superseded_by,
        }

    @staticmethod
    def _run_to_dict(r: WorkflowRun) -> dict:
        return {
            "run_id": r.run_id,
            "product_id": r.product_id,
            "signal_id": r.signal_id,
            "batch_id": r.batch_id,
            "mode": r.mode.value if r.mode else None,
            "recommendation_json": r.recommendation_json,
            "routing": r.routing.value if r.routing else None,
            "composite_score": r.composite_score,
            "created_at": r.created_at.isoformat(),
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            "prompt_tokens_total": r.prompt_tokens_total,
            "completion_tokens_total": r.completion_tokens_total,
            "attempt_no": r.attempt_no,
            "root_run_id": r.root_run_id,
            "origin": r.origin,
            # Canonical (position, lifecycle) run-state columns.
            "lifecycle": r.lifecycle,
            "position": r.position,
            "outcome": r.outcome,
            "reason": r.reason,
        }

    @staticmethod
    def _stage_output_to_dict(so: StageOutput) -> dict:
        return {
            "output_id": so.output_id,
            "run_id": so.run_id,
            "stage": so.stage,
            "output_json": so.output_json,
            "version": so.version,
            "created_at": so.created_at.isoformat(),
        }

    @staticmethod
    def _artifact_to_dict(a: Artifact) -> dict:
        return {
            "artifact_id": a.artifact_id,
            "run_id": a.run_id,
            "type": a.type.value,
            "content_md": a.content_md,
            "source_stage": a.source_stage,
            "created_at": a.created_at.isoformat(),
        }
