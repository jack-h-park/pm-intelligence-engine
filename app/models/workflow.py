import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, Enum as SAEnum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class RunStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    awaiting_direction = "awaiting_direction"
    waiting_approval = "waiting_approval"
    waiting_routing_review = "waiting_routing_review"
    completed = "completed"
    killed = "killed"
    failed = "failed"


class RunMode(str, enum.Enum):
    file = "file"              # Stage 1 only — normalize and categorize the signal
    brief = "brief"            # Stage 1 + 2 + 7 — insight extraction and brief summary
    opportunity = "opportunity"  # Stage 1 + 2 + 3 — full opportunity framing
    evaluate = "evaluate"      # Stage 1 + 2 + 3 + 4 — full evaluation, no routing
    decide = "decide"          # Stage 1–7 — full pipeline with approval gate


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
    pending = "pending"
    in_run = "in_run"
    done = "done"


class SourceType(str, enum.Enum):
    manual = "manual"
    rss = "rss"
    file_watch = "file_watch"
    web = "web"


class ApprovalAction(str, enum.Enum):
    approve = "approve"
    revise = "revise"
    reject = "reject"


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
    product_id = Column(String, nullable=False)
    title = Column(String, nullable=False)
    source_url = Column(String, nullable=True)
    raw_content = Column(Text, nullable=False)
    category = Column(SAEnum(SignalCategory), nullable=False, default=SignalCategory.other)
    status = Column(SAEnum(SignalStatus), nullable=False, default=SignalStatus.pending)
    source_type = Column(SAEnum(SourceType), nullable=False, default=SourceType.manual)
    ingested_at = Column(DateTime, nullable=False, default=_utc_now)

    runs = relationship("WorkflowRun", back_populates="signal")


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    run_id = Column(String, primary_key=True, default=_new_uuid)
    product_id = Column(String, nullable=False)
    signal_id = Column(String, ForeignKey("signals.signal_id"), nullable=False)
    status = Column(SAEnum(RunStatus), nullable=False, default=RunStatus.pending)
    current_stage = Column(String, nullable=True)
    mode = Column(SAEnum(RunMode), nullable=True)
    recommendation_json = Column(Text, nullable=True)  # S2 suggested_mode + reasoning
    routing = Column(SAEnum(Routing), nullable=True)
    composite_score = Column(Float, nullable=True)
    notification_chat_id = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utc_now)
    completed_at = Column(DateTime, nullable=True)

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
