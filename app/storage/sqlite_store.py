from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.workflow import (
    Artifact,
    ApprovalAction,
    ApprovalEvent,
    ArtifactType,
    Base,
    Routing,
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
        self._Session = sessionmaker(bind=self._engine)

    # --- Signal ---

    def save_signal(
        self,
        product_id: str,
        title: str,
        raw_content: str,
        source_url: Optional[str] = None,
        category: str = "other",
        source_type: str = "manual",
    ) -> str:
        with self._Session() as session:
            signal = Signal(
                product_id=product_id,
                title=title,
                raw_content=raw_content,
                source_url=source_url,
                category=SignalCategory(category),
                source_type=SourceType(source_type),
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
        product_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        with self._Session() as session:
            q = session.query(Signal)
            if product_id:
                q = q.filter(Signal.product_id == product_id)
            if status:
                q = q.filter(Signal.status == SignalStatus(status))
            q = q.order_by(Signal.ingested_at.desc()).limit(limit)
            return [self._signal_to_dict(s) for s in q.all()]

    # --- WorkflowRun ---

    def create_run(self, product_id: str, signal_id: str) -> str:
        with self._Session() as session:
            run = WorkflowRun(product_id=product_id, signal_id=signal_id)
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

    # --- Serializers ---

    @staticmethod
    def _signal_to_dict(s: Signal) -> dict:
        return {
            "signal_id": s.signal_id,
            "product_id": s.product_id,
            "title": s.title,
            "source_url": s.source_url,
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
            "status": r.status.value,
            "current_stage": r.current_stage,
            "mode": r.mode.value if r.mode else None,
            "recommendation_json": r.recommendation_json,
            "routing": r.routing.value if r.routing else None,
            "composite_score": r.composite_score,
            "notification_chat_id": r.notification_chat_id,
            "created_at": r.created_at.isoformat(),
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
