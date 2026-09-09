from datetime import UTC, datetime

from app.models.insights import Candidate, SourceRecord
from app.services.insight_evidence import prepare_evidence


def test_prepare_evidence_selects_seed_and_at_most_three_enrichments():
    candidate = Candidate(
        candidate_id="candidate-evidence",
        origin="user_supplied",
        subject="A practical engineering lesson",
        question_ids=["learning-loop"],
        policy_revision="fixture-v1",
    )
    sources = [
        SourceRecord(
            source_id=f"source-{number}",
            candidate_id=candidate.candidate_id,
            origin="user_supplied",
            content_hash="a" * 64,
            acquisition_status="low_quality",
            retrieved_at=datetime(2026, 9, 8, tzinfo=UTC),
        )
        for number in range(5)
    ]
    sources[0] = sources[0].model_copy(
        update={
            "acquisition_status": "ok",
            "content": "The seed source states a specific observed engineering lesson.",
            "content_hash": "b" * 64,
        }
    )
    for number in range(1, 5):
        text = f"Enrichment {number} adds a separately attributable detail."
        sources[number] = sources[number].model_copy(
            update={
                "acquisition_status": "ok",
                "content": text,
                "content_hash": (str(number) * 64),
            }
        )

    bundle = prepare_evidence(candidate, sources, context_revision="fixture-v1")

    assert bundle.source_ids == ["source-0", "source-1", "source-2", "source-3"]
    assert {passage.role for passage in bundle.passages} == {"seed", "enrichment"}


def test_missing_body_stays_an_evidence_gap():
    candidate = Candidate(
        candidate_id="thin-candidate",
        origin="user_supplied",
        subject="A learning lead without a body",
        question_ids=["learning-loop"],
        policy_revision="fixture-v1",
    )

    bundle = prepare_evidence(candidate, [], context_revision="fixture-v1")

    assert bundle.coverage_gaps
    assert bundle.source_ids == []
    assert bundle.passages == []
