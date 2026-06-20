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
    PortfolioSynthesis,
    Routing,
    RunBatch,
    RunMode,
    RunStatus,
    Signal,
    SignalCategory,
    SignalStatus,
    SourceType,
    StageOutput,
    WorkflowRun,
)


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
        (run_batches, portfolio_syntheses) are handled by ``create_all``.
        """
        inspector = inspect(self._engine)
        run_columns = {c["name"] for c in inspector.get_columns("workflow_runs")}
        if "batch_id" not in run_columns:
            with self._engine.begin() as conn:
                conn.execute(text("ALTER TABLE workflow_runs ADD COLUMN batch_id VARCHAR"))

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
        # the WHERE clauses match only legacy values, so re-running is a no-op.
        # The enum `_missing_` hooks accept the legacy strings in-code; these
        # UPDATEs migrate the stored rows so SQLAlchemy reads resolve directly.
        with self._engine.begin() as conn:
            conn.execute(
                text("UPDATE signals SET status = 'new' WHERE status = 'pending'")
            )
            conn.execute(
                text(
                    "UPDATE workflow_runs SET status = 'waiting_direction' "
                    "WHERE status = 'awaiting_direction'"
                )
            )

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

    def list_signals(
        self,
        original_product_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        with self._Session() as session:
            q = session.query(Signal)
            if original_product_id:
                q = q.filter(Signal.original_product_id == original_product_id)
            if status:
                q = q.filter(Signal.status == SignalStatus(status))
            q = q.order_by(Signal.ingested_at.desc()).limit(limit)
            return [self._signal_to_dict(s) for s in q.all()]

    # --- WorkflowRun ---

    def create_run(
        self,
        product_id: str,
        signal_id: str,
        batch_id: Optional[str] = None,
    ) -> str:
        with self._Session() as session:
            run = WorkflowRun(
                product_id=product_id,
                signal_id=signal_id,
                batch_id=batch_id,
            )
            session.add(run)
            session.commit()
            return run.run_id

    def get_run(self, run_id: str) -> Optional[dict]:
        with self._Session() as session:
            r = session.get(WorkflowRun, run_id)
            return self._run_to_dict(r) if r else None

    def update_run(self, run_id: str, **kwargs) -> None:
        with self._Session() as session:
            r = session.get(WorkflowRun, run_id)
            if not r:
                return
            for key, value in kwargs.items():
                if key == "status":
                    value = RunStatus(value)
                    # Stamp completed_at on first transition to a resolved state.
                    # failed intentionally does NOT receive completed_at — the run
                    # did not reach a meaningful endpoint and may need investigation.
                    if value in {RunStatus.completed, RunStatus.killed} and r.completed_at is None:
                        r.completed_at = datetime.now(UTC)
                elif key == "routing" and value is not None:
                    value = Routing(value)
                elif key == "mode" and value is not None:
                    value = RunMode(value)
                setattr(r, key, value)
            session.commit()

    def list_runs(
        self,
        product_id: Optional[str] = None,
        status: Optional[str] = None,
        routing: Optional[str] = None,
        event: Optional[str] = None,
        since: Optional[datetime] = None,
        batch_id: Optional[str] = None,
        signal_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        with self._Session() as session:
            q = session.query(WorkflowRun)
            if product_id:
                q = q.filter(WorkflowRun.product_id == product_id)
            if status:
                q = q.filter(WorkflowRun.status == RunStatus(status))
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
        content_json: str,
        source_stage: Optional[str] = None,
    ) -> str:
        with self._Session() as session:
            artifact = Artifact(
                run_id=run_id,
                type=ArtifactType(artifact_type),
                content_md=content_md,
                content_json=content_json,
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

    def create_batch(self, signal_id: str) -> str:
        with self._Session() as session:
            batch = RunBatch(signal_id=signal_id)
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
        }

    @staticmethod
    def _run_to_dict(r: WorkflowRun) -> dict:
        return {
            "run_id": r.run_id,
            "product_id": r.product_id,
            "signal_id": r.signal_id,
            "batch_id": r.batch_id,
            "status": r.status.value,
            "current_stage": r.current_stage,
            "mode": r.mode.value if r.mode else None,
            "recommendation_json": r.recommendation_json,
            "routing": r.routing.value if r.routing else None,
            "composite_score": r.composite_score,
            "created_at": r.created_at.isoformat(),
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
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
            "content_json": a.content_json,
            "source_stage": a.source_stage,
            "created_at": a.created_at.isoformat(),
        }
