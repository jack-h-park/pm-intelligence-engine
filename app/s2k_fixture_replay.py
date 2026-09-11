"""Run one isolated, no-cost S2K acceptance fixture against the real engine store."""

import asyncio
import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from sqlalchemy import func, select

from app.insight_worker import process_one
from app.llm.protocol import Message, Usage
from app.models.workflow import Signal, WorkflowRun
from app.services.insight_evidence import prepare_evidence
from app.storage.insight_store import InsightStore


class FixtureReplayReport(TypedDict):
    candidate_origin: str
    source_origin_reference: str
    bundle_passage_count: int
    prepared_context_validation_status: str
    job_state: str
    legacy_signals: int
    legacy_workflow_runs: int
    delivery_receipts: dict[str, int]


class _FixtureLLM:
    """A complete local response so this acceptance path never makes a provider call."""

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 2048,
        temperature: float | None = None,
        usage_sink: list[Usage] | None = None,
    ) -> str:
        return json.dumps(
            {
                "headline": "Fixture evidence supports a bounded insight",
                "explanation": "The fixture replay uses one attributable source.",
                "actual_change": "The discovered source was prepared for analysis.",
                "why_now": "The bounded fixture job is queued.",
                "personal_relevance": "It exercises the Android isolation evidence path.",
                "takeaway": "Keep the result inside the intelligence store.",
                "claims": [
                    {
                        "text": "The discovered source was prepared for analysis.",
                        "passage_ids": ["fixture-s2k-source:0"],
                    }
                ],
                "uncertainties": ["This fixture does not authorize live acquisition."],
            }
        )


async def run_fixture_replay() -> FixtureReplayReport:
    """Replay Candidate -> Source -> Bundle -> PreparedContext in an ephemeral SQLite DB."""
    with tempfile.TemporaryDirectory(prefix="pm-engine-s2k-fixture-") as directory:
        database_url = f"sqlite:///{Path(directory) / 'fixture.db'}"
        store = InsightStore(database_url)
        store.initialize_schema()

        candidate = store.save_candidate(
            {
                "origin": "discovered",
                "subject": "Android work-profile isolation evidence",
                "question_ids": ["android-enterprise-isolation"],
                "source_ids": [],
                "policy_revision": "fixture-replay-v1",
            }
        )
        content = (
            "A managed Android work profile keeps organization data separate while "
            "allowing explicitly permitted cross-profile interactions."
        )
        source = store.save_source(
            {
                "source_id": "fixture-s2k-source",
                "candidate_id": candidate.candidate_id,
                "origin": "discovered",
                "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "acquisition_status": "ok",
                "retrieved_at": datetime(2026, 9, 10, tzinfo=UTC).isoformat(),
                "content": content,
                "url": "https://fixture.example/android-work-profile",
            }
        )
        bundle = store.save_bundle(
            prepare_evidence(candidate, [source], context_revision="fixture-replay-v1").model_dump()
        )
        job = store.create_job(
            {
                "candidate_id": candidate.candidate_id,
                "bundle_id": bundle.bundle_id,
                "context_revision": "fixture-replay-v1",
                "purpose": "learning",
            }
        )
        insight = await process_one(store, _FixtureLLM())
        if insight is None:
            raise RuntimeError("fixture replay did not produce an insight")
        prepared = store.get_prepared_context(insight.prepared_context_id)
        if prepared is None:
            raise RuntimeError("fixture replay did not persist a prepared context")

        with store.engine.connect() as connection:
            legacy_signals = connection.scalar(select(func.count()).select_from(Signal))
            legacy_workflow_runs = connection.scalar(select(func.count()).select_from(WorkflowRun))
        completed_job = store.get_job(job.job_id)
        if completed_job is None:
            raise RuntimeError("fixture replay did not preserve the completed job")
        operations = store.operational_summary()
        return {
            "candidate_origin": candidate.origin,
            "source_origin_reference": source.origin_reference or "",
            "bundle_passage_count": len(bundle.passages),
            "prepared_context_validation_status": prepared.validation_status,
            "job_state": completed_job.state,
            "legacy_signals": legacy_signals or 0,
            "legacy_workflow_runs": legacy_workflow_runs or 0,
            "delivery_receipts": operations["delivery_receipts"],
        }


def main() -> None:
    print(json.dumps(asyncio.run(run_fixture_replay()), sort_keys=True))


if __name__ == "__main__":
    main()
