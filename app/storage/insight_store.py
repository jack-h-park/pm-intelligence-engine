"""Immutable SQLite-backed persistence for the E01 insight records."""

import json
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar, cast

from pydantic import BaseModel
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.models.insights import (
    BudgetReservation,
    Candidate,
    EvidenceBundle,
    InsightJob,
    InsightRevision,
    IntelligenceBudgetReservationRow,
    IntelligenceBundleRow,
    IntelligenceCandidateRow,
    IntelligenceDeliveryReceiptRow,
    IntelligenceIdempotencyRow,
    IntelligenceInsightFeedbackRow,
    IntelligenceInsightRow,
    IntelligenceJobRow,
    IntelligenceMigrationManifestRow,
    IntelligencePreparedContextRow,
    IntelligenceResearchRequestRow,
    IntelligenceResearchResultRow,
    IntelligenceSourceRow,
    IntelligenceTriageRow,
    PreparedContext,
    ResearchRequest,
    SourceRecord,
)
from app.storage.insight_migrations import initialize_insight_schema

Record = TypeVar("Record", Candidate, SourceRecord, EvidenceBundle, InsightJob)


class MissingInsightRecord(ValueError):
    pass


class InvalidInsightReference(ValueError):
    pass


class IdempotencyConflict(ValueError):
    pass


class StaleLease(ValueError):
    pass


def _json(record: BaseModel) -> str:
    return json.dumps(record.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def _now(value: datetime | None = None) -> datetime:
    return value or datetime.now(UTC)


class InsightStore:
    """A dedicated store that never reaches into ``SQLiteStore`` internals."""

    def __init__(self, database_url: str) -> None:
        self._engine = create_engine(database_url, connect_args={"check_same_thread": False})
        self._Session = sessionmaker(bind=self._engine)

    @property
    def engine(self) -> Engine:
        """Read-only test/migration access; application consumers use record methods."""
        return self._engine

    def initialize_schema(self) -> None:
        initialize_insight_schema(self._engine)

    def save_migration_manifest(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist a review-only migration overlay keyed by the immutable origin."""
        required = (
            "original_system", "original_id", "snapshot_hash", "classification",
            "migration_state", "notification_handling",
        )
        if any(not isinstance(payload.get(key), str) or not payload[key] for key in required):
            raise ValueError("migration manifest has missing required fields")
        with self._Session.begin() as session:
            row = session.execute(select(IntelligenceMigrationManifestRow).where(
                IntelligenceMigrationManifestRow.original_system == payload["original_system"],
                IntelligenceMigrationManifestRow.original_id == payload["original_id"],
            )).scalar_one_or_none()
            if row is not None:
                saved = json.loads(row.payload_json)
                if not isinstance(saved, dict):
                    raise ValueError("stored migration manifest is invalid")
                if saved["snapshot_hash"] != payload["snapshot_hash"]:
                    raise IdempotencyConflict("migration origin changed since its snapshot")
                return saved
            row = IntelligenceMigrationManifestRow(
                original_system=payload["original_system"], original_id=payload["original_id"],
                snapshot_hash=payload["snapshot_hash"], classification=payload["classification"],
                migration_state=payload["migration_state"],
                payload_json=json.dumps(payload, sort_keys=True),
            )
            session.add(row)
            return payload

    def list_migration_manifests(self) -> list[dict[str, Any]]:
        """Read migration overlays without exposing or changing legacy records."""
        with self._Session() as session:
            rows = session.execute(
                select(IntelligenceMigrationManifestRow).order_by(
                    IntelligenceMigrationManifestRow.original_id
                )
            ).scalars()
            manifests: list[dict[str, Any]] = []
            for row in rows:
                manifest = json.loads(row.payload_json)
                if not isinstance(manifest, dict):
                    raise ValueError("stored migration manifest is invalid")
                manifests.append(cast(dict[str, Any], manifest))
            return manifests

    def save_candidate(self, payload: dict[str, Any]) -> Candidate:
        candidate = Candidate.model_validate(payload)
        with self._Session.begin() as session:
            return self._save_candidate(session, candidate)

    def save_source(self, payload: dict[str, Any]) -> SourceRecord:
        source = SourceRecord.model_validate(payload)
        with self._Session.begin() as session:
            return self._save_source(session, source)[0]

    def save_bundle(self, payload: dict[str, Any]) -> EvidenceBundle:
        bundle = EvidenceBundle.model_validate(payload)
        with self._Session.begin() as session:
            return self._save_bundle(session, bundle)

    def get_bundle(self, bundle_id: str) -> EvidenceBundle | None:
        with self._Session() as session:
            row = session.get(IntelligenceBundleRow, bundle_id)
            return EvidenceBundle.model_validate_json(row.payload_json) if row else None

    def get_candidate(self, candidate_id: str) -> Candidate | None:
        with self._Session() as session:
            row = session.get(IntelligenceCandidateRow, candidate_id)
            return Candidate.model_validate_json(row.payload_json) if row else None

    def known_source_hashes(self, content_hashes: list[str]) -> list[str]:
        """Find prior immutable source bodies without exposing their content."""
        if not content_hashes:
            return []
        with self._Session() as session:
            rows = session.execute(
                select(IntelligenceSourceRow.content_hash).where(
                    IntelligenceSourceRow.content_hash.in_(content_hashes)
                )
            ).all()
            return sorted({row[0] for row in rows})

    def claim_triage(self, operation_id: str) -> tuple[str, dict[str, Any] | None]:
        """Claim one triage operation before a model call; completed calls replay safely."""
        with self._Session.begin() as session:
            row = session.get(IntelligenceTriageRow, operation_id)
            if row is not None:
                return row.state, json.loads(row.payload_json) if row.payload_json else None
            session.add(
                IntelligenceTriageRow(operation_id=operation_id, state="running", payload_json="{}")
            )
            return "claimed", None

    def complete_triage(self, operation_id: str, payload: dict[str, Any]) -> None:
        with self._Session.begin() as session:
            row = session.get(IntelligenceTriageRow, operation_id)
            if row is None:
                raise MissingInsightRecord(f"triage operation {operation_id} was not claimed")
            row.state = "complete"
            row.payload_json = json.dumps(payload, sort_keys=True)

    def abandon_triage_claim(self, operation_id: str) -> None:
        """Release a pre-call denial; ambiguous model calls intentionally stay claimed."""
        with self._Session.begin() as session:
            row = session.get(IntelligenceTriageRow, operation_id)
            if row is not None and row.state == "running":
                session.delete(row)

    # --- Prepared analysis records (E03) ---

    def save_prepared_context(self, payload: dict[str, Any]) -> PreparedContext:
        prepared = PreparedContext.model_validate(payload)
        with self._Session.begin() as session:
            if session.get(IntelligenceCandidateRow, prepared.candidate_id) is None:
                raise InvalidInsightReference(f"candidate {prepared.candidate_id} was not found")
            if session.get(IntelligenceBundleRow, prepared.bundle_id) is None:
                raise InvalidInsightReference(f"bundle {prepared.bundle_id} was not found")
            session.add(
                IntelligencePreparedContextRow(
                    prepared_context_id=prepared.prepared_context_id,
                    candidate_id=prepared.candidate_id,
                    bundle_id=prepared.bundle_id,
                    validation_status=prepared.validation_status,
                    context_revision=prepared.context_revision,
                    payload_json=_json(prepared),
                )
            )
            return prepared

    def get_prepared_context(self, prepared_context_id: str) -> PreparedContext | None:
        with self._Session() as session:
            row = session.get(IntelligencePreparedContextRow, prepared_context_id)
            return PreparedContext.model_validate_json(row.payload_json) if row else None

    def save_insight(self, payload: dict[str, Any]) -> InsightRevision:
        insight = InsightRevision.model_validate(payload)
        with self._Session.begin() as session:
            if session.get(IntelligencePreparedContextRow, insight.prepared_context_id) is None:
                raise InvalidInsightReference(
                    f"prepared context {insight.prepared_context_id} was not found"
                )
            if (
                insight.supersedes_insight_id
                and session.get(IntelligenceInsightRow, insight.supersedes_insight_id) is None
            ):
                raise InvalidInsightReference(
                    f"superseded insight {insight.supersedes_insight_id} was not found"
                )
            session.add(
                IntelligenceInsightRow(
                    insight_id=insight.insight_id,
                    prepared_context_id=insight.prepared_context_id,
                    context_revision=insight.context_revision,
                    created_at=insight.created_at,
                    payload_json=_json(insight),
                )
            )
            return insight

    def get_insight(self, insight_id: str) -> InsightRevision | None:
        with self._Session() as session:
            row = session.get(IntelligenceInsightRow, insight_id)
            return InsightRevision.model_validate_json(row.payload_json) if row else None

    def save_feedback(self, insight_id: str, revision: int, label: str) -> dict[str, Any]:
        with self._Session.begin() as session:
            if session.get(IntelligenceInsightRow, insight_id) is None:
                raise MissingInsightRecord(f"insight {insight_id} was not found")
            payload = {
                "feedback_id": str(uuid.uuid4()),
                "insight_id": insight_id,
                "revision": revision,
                "label": label,
            }
            session.add(
                IntelligenceInsightFeedbackRow(
                    feedback_id=payload["feedback_id"],
                    insight_id=insight_id,
                    revision=revision,
                    label=label,
                    payload_json=json.dumps(payload, sort_keys=True),
                )
            )
            return payload

    def list_insights(self) -> list[InsightRevision]:
        with self._Session() as session:
            rows = session.execute(
                select(IntelligenceInsightRow).order_by(IntelligenceInsightRow.created_at.desc())
            ).scalars()
            return [InsightRevision.model_validate_json(row.payload_json) for row in rows]

    def list_current_insights(self) -> list[InsightRevision]:
        insights = self.list_insights()
        superseded = {insight.supersedes_insight_id for insight in insights}
        return [insight for insight in insights if insight.insight_id not in superseded]

    def operational_summary(self) -> dict[str, Any]:
        """Return read-only counts for shadow operations without admitting work."""
        with self._Session() as session:
            jobs = Counter(
                row.state for row in session.execute(select(IntelligenceJobRow)).scalars()
            )
            receipts = Counter(
                row.state
                for row in session.execute(select(IntelligenceDeliveryReceiptRow)).scalars()
            )
            reservations = [
                BudgetReservation.model_validate_json(row.payload_json)
                for row in session.execute(select(IntelligenceBudgetReservationRow)).scalars()
            ]
            return {
                "candidates": session.query(IntelligenceCandidateRow).count(),
                "jobs": dict(sorted(jobs.items())),
                "delivery_receipts": dict(sorted(receipts.items())),
                "feedback": {
                    "recorded": session.query(IntelligenceInsightFeedbackRow).count(),
                    "unknown": 0,
                },
                "cost_micros": {
                    "reserved": sum(
                        item.maximum_micros
                        for item in reservations
                        if item.state in {"reserved", "unknown"}
                    ),
                    "finalized": sum(item.actual_micros or 0 for item in reservations),
                    "unknown": sum(
                        item.maximum_micros for item in reservations if item.state == "unknown"
                    ),
                },
            }

    def save_delivery_receipt(
        self, insight_id: str, revision: int, channel: str, state: str
    ) -> dict[str, Any]:
        """Persist a channel receipt once; callers reconcile uncertainty instead of resending."""
        with self._Session.begin() as session:
            if session.get(IntelligenceInsightRow, insight_id) is None:
                raise MissingInsightRecord(f"insight {insight_id} was not found")
            existing = session.scalar(
                select(IntelligenceDeliveryReceiptRow).where(
                    IntelligenceDeliveryReceiptRow.insight_id == insight_id,
                    IntelligenceDeliveryReceiptRow.revision == revision,
                    IntelligenceDeliveryReceiptRow.channel == channel,
                )
            )
            new_payload: dict[str, Any]
            if existing:
                new_payload = json.loads(existing.payload_json)
                # A transport may be confirmed only after the queue record was
                # committed.  Preserve that one-way acknowledgement, while an
                # ambiguous result remains a deliberate operator hold rather
                # than a signal to resend the same revision.
                if existing.state == "queued" and state in {"sent", "unknown"}:
                    new_payload["state"] = state
                    existing.state = state
                    existing.payload_json = json.dumps(new_payload, sort_keys=True)
                return new_payload
            new_payload = {
                "receipt_id": str(uuid.uuid4()),
                "insight_id": insight_id,
                "revision": revision,
                "channel": channel,
                "state": state,
            }
            session.add(
                IntelligenceDeliveryReceiptRow(
                    receipt_id=new_payload["receipt_id"],
                    insight_id=insight_id,
                    revision=revision,
                    channel=channel,
                    state=state,
                    payload_json=json.dumps(new_payload, sort_keys=True),
                )
            )
            return new_payload

    def complete_job_analysis(
        self,
        job_id: str,
        lease_token: str,
        prepared: PreparedContext,
        insight: InsightRevision,
        now: datetime | None = None,
    ) -> InsightRevision:
        """Persist analysis and job completion in one transaction under the active lease."""
        current = _now(now)
        with self._Session.begin() as session:
            job_row = session.get(IntelligenceJobRow, job_id)
            if job_row is None:
                raise MissingInsightRecord(f"job {job_id} was not found")
            job = InsightJob.model_validate_json(job_row.payload_json)
            if job.state != "running" or job.lease_token != lease_token:
                raise StaleLease("job lease is stale")
            if job.bundle_id != prepared.bundle_id or prepared.candidate_id != job.candidate_id:
                raise InvalidInsightReference("prepared context does not match the leased job")
            if insight.prepared_context_id != prepared.prepared_context_id:
                raise InvalidInsightReference("insight does not reference the prepared context")
            session.add(
                IntelligencePreparedContextRow(
                    prepared_context_id=prepared.prepared_context_id,
                    candidate_id=prepared.candidate_id,
                    bundle_id=prepared.bundle_id,
                    validation_status=prepared.validation_status,
                    context_revision=prepared.context_revision,
                    payload_json=_json(prepared),
                )
            )
            session.add(
                IntelligenceInsightRow(
                    insight_id=insight.insight_id,
                    prepared_context_id=insight.prepared_context_id,
                    context_revision=insight.context_revision,
                    created_at=insight.created_at,
                    payload_json=_json(insight),
                )
            )
            job.prepared_context_id = prepared.prepared_context_id
            job.state = "complete"
            job.completion_disposition = "ready"
            job.lease_token = None
            job.lease_expires_at = None
            job.updated_at = current
            self._write_job(job_row, job)
            return insight

    def complete_job_needs_evidence(
        self,
        job_id: str,
        lease_token: str,
        reason: str = "No evidence bundle is available for analysis",
        now: datetime | None = None,
    ) -> InsightJob:
        """Close an unanalysable job explicitly instead of stranding its lease."""
        current = _now(now)
        with self._Session.begin() as session:
            row = session.get(IntelligenceJobRow, job_id)
            if row is None:
                raise MissingInsightRecord(f"job {job_id} was not found")
            job = InsightJob.model_validate_json(row.payload_json)
            if job.state != "running" or job.lease_token != lease_token:
                raise StaleLease("job lease is stale")
            job.state = "complete"
            job.completion_disposition = "needs_evidence"
            job.lease_token = None
            job.lease_expires_at = None
            job.error = reason[:1000]
            job.updated_at = current
            self._write_job(row, job)
            return job

    def complete_job_no_new_learning(
        self, job_id: str, lease_token: str, reason: str, now: datetime | None = None
    ) -> InsightJob:
        """Close an explicit duplicate/stale result without creating an Insight."""
        current = _now(now)
        with self._Session.begin() as session:
            row = session.get(IntelligenceJobRow, job_id)
            if row is None:
                raise MissingInsightRecord(f"job {job_id} was not found")
            job = InsightJob.model_validate_json(row.payload_json)
            if job.state != "running" or job.lease_token != lease_token:
                raise StaleLease("job lease is stale")
            job.state = "complete"
            job.completion_disposition = "no_new_learning"
            job.lease_token = None
            job.lease_expires_at = None
            job.error = reason[:1000]
            job.updated_at = current
            self._write_job(row, job)
            return job

    def fail_job_retryable(
        self, job_id: str, lease_token: str, error: str, now: datetime | None = None
    ) -> InsightJob:
        """Release a failed worker lease so a bounded later tick can retry it."""
        current = _now(now)
        with self._Session.begin() as session:
            row = session.get(IntelligenceJobRow, job_id)
            if row is None:
                raise MissingInsightRecord(f"job {job_id} was not found")
            job = InsightJob.model_validate_json(row.payload_json)
            if job.state != "running" or job.lease_token != lease_token:
                raise StaleLease("job lease is stale")
            job.state = "retryable_failed"
            job.lease_token = None
            job.lease_expires_at = None
            job.next_attempt_at = current
            job.error = error[:1000]
            job.updated_at = current
            self._write_job(row, job)
            return job

    # --- Jobs and acquisition research (E02) ---

    def create_job(self, payload: dict[str, Any]) -> InsightJob:
        job = InsightJob.model_validate(payload)
        with self._Session.begin() as session:
            if session.get(IntelligenceCandidateRow, job.candidate_id) is None:
                raise MissingInsightRecord(f"candidate {job.candidate_id} was not found")
            if job.bundle_id and session.get(IntelligenceBundleRow, job.bundle_id) is None:
                raise InvalidInsightReference(f"bundle {job.bundle_id} was not found")
            session.add(
                IntelligenceJobRow(
                    job_id=job.job_id,
                    candidate_id=job.candidate_id,
                    state=job.state,
                    next_attempt_at=job.next_attempt_at,
                    lease_token=job.lease_token,
                    lease_expires_at=job.lease_expires_at,
                    payload_json=_json(job),
                )
            )
            return job

    def create_idempotent_job(
        self, actor: str, key: str, request_hash: str, payload: dict[str, Any]
    ) -> tuple[InsightJob, int]:
        job = InsightJob.model_validate(payload)

        def save(session: Session) -> tuple[InsightJob, bool]:
            if session.get(IntelligenceCandidateRow, job.candidate_id) is None:
                raise MissingInsightRecord(f"candidate {job.candidate_id} was not found")
            if job.bundle_id and session.get(IntelligenceBundleRow, job.bundle_id) is None:
                raise InvalidInsightReference(f"bundle {job.bundle_id} was not found")
            session.add(
                IntelligenceJobRow(
                    job_id=job.job_id,
                    candidate_id=job.candidate_id,
                    state=job.state,
                    next_attempt_at=job.next_attempt_at,
                    lease_token=job.lease_token,
                    lease_expires_at=job.lease_expires_at,
                    payload_json=_json(job),
                )
            )
            return job, True

        return self.create_idempotent("job", actor, key, request_hash, InsightJob, save)

    def get_job(self, job_id: str) -> InsightJob | None:
        with self._Session() as session:
            row = session.get(IntelligenceJobRow, job_id)
            return InsightJob.model_validate_json(row.payload_json) if row else None

    def claim_job(self, now: datetime | None = None) -> InsightJob | None:
        current = _now(now)
        with self._Session.begin() as session:
            expired = (
                session.execute(
                    select(IntelligenceJobRow).where(
                        IntelligenceJobRow.state == "running",
                        IntelligenceJobRow.lease_expires_at <= current,
                    )
                )
                .scalars()
                .all()
            )
            for row in expired:
                job = InsightJob.model_validate_json(row.payload_json)
                job.state = "queued"
                job.lease_token = None
                job.lease_expires_at = None
                job.error = "Worker lease expired before completion"
                job.next_attempt_at = current
                job.updated_at = current
                self._write_job(row, job)
            claimable = (
                session.execute(
                    select(IntelligenceJobRow)
                    .where(
                        IntelligenceJobRow.state.in_(["queued", "retryable_failed"]),
                        (IntelligenceJobRow.next_attempt_at.is_(None))
                        | (IntelligenceJobRow.next_attempt_at <= current),
                    )
                    .order_by(IntelligenceJobRow.job_id)
                )
                .scalars()
                .first()
            )
            if claimable is None:
                return None
            job = InsightJob.model_validate_json(claimable.payload_json)
            job.state = "running"
            job.attempt_count += 1
            job.lease_token = str(uuid.uuid4())
            job.lease_expires_at = current + timedelta(seconds=120)
            job.next_attempt_at = None
            job.updated_at = current
            self._write_job(claimable, job)
            return job

    def create_research_request(
        self,
        job_id: str,
        lease_token: str,
        targets: list[str],
        questions: list[str],
        maximum_fetch_count: int,
        now: datetime | None = None,
    ) -> ResearchRequest:
        current = _now(now)
        with self._Session.begin() as session:
            row = session.get(IntelligenceJobRow, job_id)
            if row is None:
                raise MissingInsightRecord(f"job {job_id} was not found")
            job = InsightJob.model_validate_json(row.payload_json)
            if (
                job.state != "running"
                or job.lease_token != lease_token
                or (job.lease_expires_at and job.lease_expires_at <= current)
            ):
                raise StaleLease("job lease is stale")
            request = ResearchRequest(
                job_id=job_id,
                parent_lease_token=lease_token,
                targets=targets,
                questions=questions,
                maximum_fetch_count=maximum_fetch_count,
                expires_at=current + timedelta(minutes=10),
            )
            job.state = "waiting_research"
            job.lease_token = None
            job.lease_expires_at = None
            job.updated_at = current
            self._write_job(row, job)
            session.add(
                IntelligenceResearchRequestRow(
                    research_request_id=request.research_request_id,
                    job_id=request.job_id,
                    state=request.state,
                    expires_at=request.expires_at,
                    lease_token=None,
                    payload_json=_json(request),
                )
            )
            return request

    def get_research_request(self, request_id: str) -> ResearchRequest | None:
        with self._Session() as session:
            row = session.get(IntelligenceResearchRequestRow, request_id)
            return ResearchRequest.model_validate_json(row.payload_json) if row else None

    def claim_research(
        self, adapter_id: str, now: datetime | None = None
    ) -> ResearchRequest | None:
        current = _now(now)
        with self._Session.begin() as session:
            row = (
                session.execute(
                    select(IntelligenceResearchRequestRow)
                    .where(
                        IntelligenceResearchRequestRow.state == "queued",
                        IntelligenceResearchRequestRow.expires_at > current,
                    )
                    .order_by(IntelligenceResearchRequestRow.research_request_id)
                )
                .scalars()
                .first()
            )
            if row is None:
                return None
            request = ResearchRequest.model_validate_json(row.payload_json)
            request.state = "running"
            request.adapter_id = adapter_id
            request.lease_token = str(uuid.uuid4())
            request.lease_expires_at = current + timedelta(seconds=120)
            self._write_research(row, request)
            return request

    def submit_research_results(
        self,
        request_id: str,
        lease_token: str,
        results: list[dict[str, Any]],
        failures: list[dict[str, Any]],
        now: datetime | None = None,
    ) -> ResearchRequest:
        current = _now(now)
        with self._Session.begin() as session:
            row = session.get(IntelligenceResearchRequestRow, request_id)
            if row is None:
                raise MissingInsightRecord(f"research request {request_id} was not found")
            request = ResearchRequest.model_validate_json(row.payload_json)
            if request.state == "complete":
                return request
            if request.state != "running" or request.lease_token != lease_token:
                raise StaleLease("research lease is stale")
            for result in results:
                content_hash = str(result.get("content_hash", ""))
                if len(content_hash) != 64:
                    raise ValueError("research result requires a content_hash")
                exists = session.execute(
                    select(IntelligenceResearchResultRow).where(
                        IntelligenceResearchResultRow.research_request_id == request_id,
                        IntelligenceResearchResultRow.content_hash == content_hash,
                    )
                ).scalar_one_or_none()
                if exists is None:
                    session.add(
                        IntelligenceResearchResultRow(
                            research_request_id=request_id,
                            content_hash=content_hash,
                            payload_json=json.dumps(result, sort_keys=True),
                        )
                    )
            request.state = "complete"
            request.completed_at = current
            request.lease_token = None
            request.lease_expires_at = None
            self._write_research(row, request)
            job_row = session.get(IntelligenceJobRow, request.job_id)
            if job_row is not None:
                job = InsightJob.model_validate_json(job_row.payload_json)
                if job.state == "waiting_research":
                    job.state = "queued"
                    job.error = None
                    job.next_attempt_at = current
                    job.updated_at = current
                    self._write_job(job_row, job)
            return request

    def expire_research_requests(self, now: datetime | None = None) -> None:
        current = _now(now)
        with self._Session.begin() as session:
            rows = (
                session.execute(
                    select(IntelligenceResearchRequestRow).where(
                        IntelligenceResearchRequestRow.state.in_(["queued", "running"]),
                        IntelligenceResearchRequestRow.expires_at <= current,
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                request = ResearchRequest.model_validate_json(row.payload_json)
                request.state = "expired"
                request.lease_token = None
                request.lease_expires_at = None
                self._write_research(row, request)
                job_row = session.get(IntelligenceJobRow, request.job_id)
                if job_row is not None:
                    job = InsightJob.model_validate_json(job_row.payload_json)
                    if job.state == "waiting_research":
                        job.state = "queued"
                        job.error = "Research request expired; evidence gap retained"
                        job.next_attempt_at = current
                        job.updated_at = current
                        self._write_job(job_row, job)

    # --- Budget reservations (E02) ---

    def reserve_budget(
        self, payload: dict[str, Any], allowance_micros: int
    ) -> BudgetReservation | None:
        reservation = BudgetReservation.model_validate(payload)
        # SQLite's deferred transactions allow two workers to read the same
        # remaining allowance before either writes. Acquire the write lock
        # before computing the reservation total so the check and insert are
        # one serializable operation.
        with self._engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            session = Session(bind=connection)
            try:
                existing = session.execute(
                    select(IntelligenceBudgetReservationRow).where(
                        IntelligenceBudgetReservationRow.operation_id == reservation.operation_id
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    connection.commit()
                    return BudgetReservation.model_validate_json(existing.payload_json)
                reserved = sum(
                    row.maximum_micros
                    for row in session.execute(
                        select(IntelligenceBudgetReservationRow).where(
                            IntelligenceBudgetReservationRow.allowance_class
                            == reservation.allowance_class,
                            IntelligenceBudgetReservationRow.state.in_(["reserved", "unknown"]),
                        )
                    ).scalars()
                )
                if reserved + reservation.maximum_micros > allowance_micros:
                    connection.commit()
                    return None
                session.add(
                    IntelligenceBudgetReservationRow(
                        reservation_id=reservation.reservation_id,
                        operation_id=reservation.operation_id,
                        allowance_class=reservation.allowance_class,
                        state=reservation.state,
                        maximum_micros=reservation.maximum_micros,
                        payload_json=_json(reservation),
                    )
                )
                session.flush()
                connection.commit()
                return reservation
            except Exception:
                connection.rollback()
                raise
            finally:
                session.close()

    def finalize_budget(
        self, reservation_id: str, actual_micros: int | str
    ) -> BudgetReservation | None:
        with self._Session.begin() as session:
            row = session.get(IntelligenceBudgetReservationRow, reservation_id)
            if row is None:
                return None
            reservation = BudgetReservation.model_validate_json(row.payload_json)
            if reservation.state != "reserved":
                return reservation
            reservation.state = "unknown" if actual_micros == "unknown" else "finalized"
            reservation.actual_micros = None if actual_micros == "unknown" else int(actual_micros)
            reservation.finalized_at = datetime.now(UTC)
            row.state = reservation.state
            row.payload_json = _json(reservation)
            return reservation

    @staticmethod
    def _write_job(row: IntelligenceJobRow, job: InsightJob) -> None:
        row.state = job.state
        row.next_attempt_at = job.next_attempt_at
        row.lease_token = job.lease_token
        row.lease_expires_at = job.lease_expires_at
        row.payload_json = _json(job)

    @staticmethod
    def _write_research(row: IntelligenceResearchRequestRow, request: ResearchRequest) -> None:
        row.state = request.state
        row.expires_at = request.expires_at
        row.lease_token = request.lease_token
        row.payload_json = _json(request)

    def create_idempotent(
        self,
        operation: str,
        actor: str,
        idempotency_key: str,
        request_hash: str,
        record_type: type[Record],
        save: Callable[[Session], tuple[Record, bool]],
    ) -> tuple[Record, int]:
        """Persist an operation result with its canonical request hash atomically."""
        with self._Session.begin() as session:
            existing = session.execute(
                select(IntelligenceIdempotencyRow).where(
                    IntelligenceIdempotencyRow.operation == operation,
                    IntelligenceIdempotencyRow.actor == actor,
                    IntelligenceIdempotencyRow.idempotency_key == idempotency_key,
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise IdempotencyConflict(
                        "Idempotency-Key was already used with different content"
                    )
                # A replay is successful but distinguishable from the operation
                # that created the record, as required by the HTTP contract.
                return record_type.model_validate_json(existing.response_json), 200

            record, created = save(session)
            status_code = 201 if created else 200
            session.add(
                IntelligenceIdempotencyRow(
                    operation=operation,
                    actor=actor,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    status_code=status_code,
                    response_json=_json(record),
                )
            )
            return record, status_code

    def save_idempotent_candidate(
        self, actor: str, key: str, request_hash: str, payload: dict[str, Any]
    ) -> tuple[Candidate, int]:
        candidate = Candidate.model_validate(payload)
        return self.create_idempotent(
            "candidate",
            actor,
            key,
            request_hash,
            Candidate,
            lambda session: (self._save_candidate(session, candidate), True),
        )

    def save_idempotent_source(
        self, actor: str, key: str, request_hash: str, payload: dict[str, Any]
    ) -> tuple[SourceRecord, int]:
        source = SourceRecord.model_validate(payload)
        return self.create_idempotent(
            "source",
            actor,
            key,
            request_hash,
            SourceRecord,
            lambda session: self._save_source(session, source),
        )

    def save_idempotent_bundle(
        self, actor: str, key: str, request_hash: str, payload: dict[str, Any]
    ) -> tuple[EvidenceBundle, int]:
        bundle = EvidenceBundle.model_validate(payload)
        return self.create_idempotent(
            "bundle",
            actor,
            key,
            request_hash,
            EvidenceBundle,
            lambda session: (self._save_bundle(session, bundle), True),
        )

    @staticmethod
    def _save_candidate(session: Session, candidate: Candidate) -> Candidate:
        session.add(
            IntelligenceCandidateRow(
                candidate_id=candidate.candidate_id,
                origin=candidate.origin,
                created_at=candidate.created_at,
                policy_revision=candidate.policy_revision,
                payload_json=_json(candidate),
            )
        )
        return candidate

    @staticmethod
    def _save_source(session: Session, source: SourceRecord) -> tuple[SourceRecord, bool]:
        if session.get(IntelligenceCandidateRow, source.candidate_id) is None:
            raise MissingInsightRecord(f"candidate {source.candidate_id} was not found")
        existing = session.execute(
            select(IntelligenceSourceRow).where(
                IntelligenceSourceRow.candidate_id == source.candidate_id,
                IntelligenceSourceRow.content_hash == source.content_hash,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return SourceRecord.model_validate_json(existing.payload_json), False
        session.add(
            IntelligenceSourceRow(
                source_id=source.source_id,
                candidate_id=source.candidate_id,
                content_hash=source.content_hash,
                url=source.url,
                retrieved_at=source.retrieved_at,
                payload_json=_json(source),
            )
        )
        return source, True

    @staticmethod
    def _save_bundle(session: Session, bundle: EvidenceBundle) -> EvidenceBundle:
        if session.get(IntelligenceCandidateRow, bundle.candidate_id) is None:
            raise InvalidInsightReference(f"candidate {bundle.candidate_id} was not found")
        source_count = session.execute(
            select(IntelligenceSourceRow.source_id).where(
                IntelligenceSourceRow.candidate_id == bundle.candidate_id,
                IntelligenceSourceRow.source_id.in_(bundle.source_ids),
            )
        ).all()
        if {row[0] for row in source_count} != set(bundle.source_ids):
            raise InvalidInsightReference("bundle references a missing source")
        session.add(
            IntelligenceBundleRow(
                bundle_id=bundle.bundle_id,
                candidate_id=bundle.candidate_id,
                context_revision=bundle.context_revision,
                created_at=bundle.created_at,
                payload_json=_json(bundle),
            )
        )
        return bundle
