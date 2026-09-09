"""Immutable SQLite-backed persistence for the E01 insight records."""

import json
from collections.abc import Callable
from typing import TypeVar

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.models.insights import (
    Candidate,
    EvidenceBundle,
    IntelligenceBundleRow,
    IntelligenceCandidateRow,
    IntelligenceIdempotencyRow,
    IntelligenceSourceRow,
    SourceRecord,
)
from app.storage.insight_migrations import initialize_insight_schema

Record = TypeVar("Record", Candidate, SourceRecord, EvidenceBundle)


class MissingInsightRecord(ValueError):
    pass


class InvalidInsightReference(ValueError):
    pass


class IdempotencyConflict(ValueError):
    pass


def _json(record: Candidate | SourceRecord | EvidenceBundle) -> str:
    return json.dumps(record.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


class InsightStore:
    """A dedicated store that never reaches into ``SQLiteStore`` internals."""

    def __init__(self, database_url: str) -> None:
        self._engine = create_engine(database_url, connect_args={"check_same_thread": False})
        self._Session = sessionmaker(bind=self._engine)

    @property
    def engine(self):
        """Read-only test/migration access; application consumers use record methods."""
        return self._engine

    def initialize_schema(self) -> None:
        initialize_insight_schema(self._engine)

    def save_candidate(self, payload: dict) -> Candidate:
        candidate = Candidate.model_validate(payload)
        with self._Session.begin() as session:
            return self._save_candidate(session, candidate)

    def save_source(self, payload: dict) -> SourceRecord:
        source = SourceRecord.model_validate(payload)
        with self._Session.begin() as session:
            return self._save_source(session, source)[0]

    def save_bundle(self, payload: dict) -> EvidenceBundle:
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
        self, actor: str, key: str, request_hash: str, payload: dict
    ) -> tuple[Candidate, int]:
        candidate = Candidate.model_validate(payload)
        return self.create_idempotent(
            "candidate", actor, key, request_hash, Candidate,
            lambda session: (self._save_candidate(session, candidate), True),
        )

    def save_idempotent_source(
        self, actor: str, key: str, request_hash: str, payload: dict
    ) -> tuple[SourceRecord, int]:
        source = SourceRecord.model_validate(payload)
        return self.create_idempotent(
            "source", actor, key, request_hash, SourceRecord,
            lambda session: self._save_source(session, source),
        )

    def save_idempotent_bundle(
        self, actor: str, key: str, request_hash: str, payload: dict
    ) -> tuple[EvidenceBundle, int]:
        bundle = EvidenceBundle.model_validate(payload)
        return self.create_idempotent(
            "bundle", actor, key, request_hash, EvidenceBundle,
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
