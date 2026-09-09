"""Immutable record contracts for Personal Signal Intelligence.

The Pydantic records are the public storage/API contract.  Their SQLAlchemy
counterparts retain the complete canonical JSON payload while exposing the
identity and reference columns that later slices will index.
"""

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint

from app.models.workflow import Base


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _utc_now() -> datetime:
    return datetime.now(UTC)


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1


class Candidate(_Record):
    candidate_id: str = Field(default_factory=_new_uuid)
    origin: Literal["discovered", "user_supplied", "legacy_import"]
    subject: str = Field(min_length=1)
    question_ids: list[str]
    source_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utc_now)
    policy_revision: str = Field(min_length=1)


class SourceExcerpt(_Record):
    text: str = Field(min_length=1)
    locator: str | None = None


class SourceRecord(_Record):
    source_id: str = Field(default_factory=_new_uuid)
    candidate_id: str
    origin: Literal["discovered", "user_supplied", "legacy_import"]
    content_hash: str = Field(min_length=64, max_length=64)
    acquisition_status: Literal["ok", "fallback_summary", "low_quality", "fetch_failed"]
    retrieved_at: datetime
    content: str | None = None
    excerpts: list[SourceExcerpt] = Field(default_factory=list)
    url: str | None = None
    legacy_reference: str | None = None

    @field_validator("retrieved_at")
    @classmethod
    def _retrieved_at_is_utc_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("retrieved_at must be timezone-aware UTC")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _successful_source_has_material(self) -> "SourceRecord":
        if self.acquisition_status in {"ok", "fallback_summary"} and not (
            self.content and self.content.strip()
        ) and not self.excerpts:
            raise ValueError("successful source requires content or at least one permitted excerpt")
        if self.content is not None and not self.content.strip():
            raise ValueError("content must not be empty")
        content_hash = (
            hashlib.sha256(self.content.encode("utf-8")).hexdigest()
            if self.content is not None
            else None
        )
        if content_hash is not None and content_hash != self.content_hash:
            raise ValueError("content_hash does not match content")
        return self


class Passage(_Record):
    passage_id: str
    source_id: str
    locator: str
    text: str = Field(min_length=1)
    role: Literal["seed", "enrichment"]


class EvidenceDate(_Record):
    value: str | None
    kind: str = Field(min_length=1)
    provenance: str = Field(min_length=1)


class EvidenceBundle(_Record):
    bundle_id: str = Field(default_factory=_new_uuid)
    candidate_id: str
    source_ids: list[str] = Field(max_length=4)
    passages: list[Passage]
    dates: list[EvidenceDate] = Field(default_factory=list)
    coverage_gaps: list[str] = Field(default_factory=list)
    freshness_status: Literal["current", "background", "superseded", "unknown"]
    context_revision: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=_utc_now)

    @property
    def passage_ids(self) -> set[str]:
        return {passage.passage_id for passage in self.passages}

    @model_validator(mode="after")
    def _passages_reference_bundle_sources(self) -> "EvidenceBundle":
        sources = set(self.source_ids)
        if len(sources) != len(self.source_ids):
            raise ValueError("bundle source_ids must be unique")
        if any(passage.source_id not in sources for passage in self.passages):
            raise ValueError("passage source must be listed in bundle source_ids")
        if len(self.passage_ids) != len(self.passages):
            raise ValueError("passage_ids must be unique")
        return self


class PreparedFact(_Record):
    text: str = Field(min_length=1)
    passage_ids: list[str] = Field(min_length=1)


class PreparedContext(_Record):
    prepared_context_id: str = Field(default_factory=_new_uuid)
    revision: int = Field(default=1, ge=1)
    candidate_id: str
    bundle_id: str
    question: str = Field(min_length=1)
    facts: list[PreparedFact] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    validation_status: Literal["valid", "needs_evidence"]
    context_revision: str = Field(min_length=1)
    context_paths: list[str] = Field(default_factory=list)
    context_hashes: dict[str, str] = Field(default_factory=dict)
    relevance_reasons: list[str] = Field(default_factory=list)
    note_connections: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utc_now)


class InsightClaim(_Record):
    text: str = Field(min_length=1)
    passage_ids: list[str] = Field(min_length=1)


class InsightRevision(_Record):
    insight_id: str = Field(default_factory=_new_uuid)
    revision: int = Field(default=1, ge=1)
    prepared_context_id: str
    headline: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    actual_change: str = Field(min_length=1)
    why_now: str = Field(min_length=1)
    personal_relevance: str = Field(min_length=1)
    takeaway: str = Field(min_length=1)
    claims: list[InsightClaim] = Field(min_length=1)
    uncertainties: list[str] = Field(default_factory=list)
    question_ids: list[str] = Field(default_factory=list)
    related_insight_ids: list[str] = Field(default_factory=list)
    supersedes_insight_id: str | None = None
    note_connections: list[str] = Field(default_factory=list)
    event_cluster_id: str | None = None
    generation_model: str | None = None
    context_revision: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=_utc_now)


class InsightJob(_Record):
    job_id: str = Field(default_factory=_new_uuid)
    candidate_id: str
    bundle_id: str | None = None
    prepared_context_id: str | None = None
    context_revision: str
    purpose: Literal["learning", "decision_preparation"]
    state: Literal[
        "queued", "running", "waiting_research", "retryable_failed", "complete", "exhausted"
    ] = "queued"
    attempt_count: int = 0
    next_attempt_at: datetime | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    error: str | None = None
    completion_disposition: (
        Literal["ready", "prepared", "needs_evidence", "no_new_learning"] | None
    ) = None
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


class ResearchRequest(_Record):
    research_request_id: str = Field(default_factory=_new_uuid)
    job_id: str
    parent_lease_token: str
    targets: list[str] = Field(min_length=1)
    questions: list[str] = Field(min_length=1)
    maximum_fetch_count: int = Field(ge=1, le=3)
    state: Literal["queued", "running", "complete", "expired"] = "queued"
    adapter_id: str | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    expires_at: datetime
    created_at: datetime = Field(default_factory=_utc_now)
    completed_at: datetime | None = None


class BudgetReservation(_Record):
    reservation_id: str = Field(default_factory=_new_uuid)
    operation_id: str
    operation_type: str
    candidate_id: str | None = None
    job_id: str | None = None
    policy_revision: str
    provider: str
    rate_revision: str
    maximum_micros: int = Field(gt=0)
    allowance_class: str
    state: Literal["reserved", "finalized", "unknown"] = "reserved"
    actual_micros: int | None = None
    created_at: datetime = Field(default_factory=_utc_now)
    finalized_at: datetime | None = None


class IntelligenceCandidateRow(Base):
    __tablename__ = "intelligence_candidates"

    candidate_id = Column(String, primary_key=True)
    origin = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now)
    policy_revision = Column(String, nullable=False)
    payload_json = Column(Text, nullable=False)


class IntelligenceSourceRow(Base):
    __tablename__ = "intelligence_sources"
    __table_args__ = (
        UniqueConstraint("candidate_id", "content_hash", name="uq_intelligence_source_hash"),
    )

    source_id = Column(String, primary_key=True)
    candidate_id = Column(String, nullable=False, index=True)
    content_hash = Column(String, nullable=False)
    url = Column(String, nullable=True)
    retrieved_at = Column(DateTime(timezone=True), nullable=False)
    payload_json = Column(Text, nullable=False)


class IntelligenceBundleRow(Base):
    __tablename__ = "intelligence_bundles"

    bundle_id = Column(String, primary_key=True)
    candidate_id = Column(String, nullable=False, index=True)
    context_revision = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now)
    payload_json = Column(Text, nullable=False)


class IntelligencePreparedContextRow(Base):
    __tablename__ = "intelligence_prepared_contexts"

    prepared_context_id = Column(String, primary_key=True)
    candidate_id = Column(String, nullable=False, index=True)
    bundle_id = Column(String, nullable=False, index=True)
    validation_status = Column(String, nullable=False, index=True)
    context_revision = Column(String, nullable=False)
    payload_json = Column(Text, nullable=False)


class IntelligenceInsightRow(Base):
    __tablename__ = "intelligence_insights"

    insight_id = Column(String, primary_key=True)
    prepared_context_id = Column(String, nullable=False, index=True)
    context_revision = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now)
    payload_json = Column(Text, nullable=False)


class IntelligenceSchemaVersionRow(Base):
    __tablename__ = "intelligence_schema_versions"

    version = Column(Integer, primary_key=True)
    applied_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now)


class IntelligenceIdempotencyRow(Base):
    __tablename__ = "intelligence_idempotency"
    __table_args__ = (
        UniqueConstraint(
            "operation", "actor", "idempotency_key", name="uq_intelligence_idempotency"
        ),
    )

    operation = Column(String, primary_key=True)
    actor = Column(String, primary_key=True)
    idempotency_key = Column(String, primary_key=True)
    request_hash = Column(String, nullable=False)
    status_code = Column(Integer, nullable=False)
    response_json = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now)


class IntelligenceJobRow(Base):
    __tablename__ = "intelligence_jobs"

    job_id = Column(String, primary_key=True)
    candidate_id = Column(String, nullable=False, index=True)
    state = Column(String, nullable=False, index=True)
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    lease_token = Column(String, nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    payload_json = Column(Text, nullable=False)


class IntelligenceResearchRequestRow(Base):
    __tablename__ = "intelligence_research_requests"

    research_request_id = Column(String, primary_key=True)
    job_id = Column(String, nullable=False, index=True)
    state = Column(String, nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    lease_token = Column(String, nullable=True)
    payload_json = Column(Text, nullable=False)


class IntelligenceResearchResultRow(Base):
    __tablename__ = "intelligence_research_results"
    __table_args__ = (
        UniqueConstraint(
            "research_request_id", "content_hash", name="uq_intelligence_research_result"
        ),
    )

    result_id = Column(String, primary_key=True, default=_new_uuid)
    research_request_id = Column(String, nullable=False, index=True)
    content_hash = Column(String, nullable=False)
    payload_json = Column(Text, nullable=False)


class IntelligenceBudgetReservationRow(Base):
    __tablename__ = "intelligence_budget_reservations"

    reservation_id = Column(String, primary_key=True)
    operation_id = Column(String, nullable=False, unique=True)
    allowance_class = Column(String, nullable=False, index=True)
    state = Column(String, nullable=False, index=True)
    maximum_micros = Column(Integer, nullable=False)
    payload_json = Column(Text, nullable=False)


class IntelligenceDeliveryReceiptRow(Base):
    __tablename__ = "intelligence_delivery_receipts"
    __table_args__ = (
        UniqueConstraint("insight_id", "revision", "channel", name="uq_intelligence_delivery"),
    )

    receipt_id = Column(String, primary_key=True)
    insight_id = Column(String, nullable=False, index=True)
    revision = Column(Integer, nullable=False)
    channel = Column(String, nullable=False)
    state = Column(String, nullable=False, index=True)
    payload_json = Column(Text, nullable=False)


class IntelligenceTriageRow(Base):
    __tablename__ = "intelligence_triage"

    operation_id = Column(String, primary_key=True)
    state = Column(String, nullable=False, index=True)
    payload_json = Column(Text, nullable=False)
