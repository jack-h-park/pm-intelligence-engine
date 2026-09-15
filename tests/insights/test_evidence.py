import hashlib
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


def test_prepare_evidence_splits_a_long_source_into_ordered_passages():
    candidate = Candidate(
        candidate_id="paragraph-candidate",
        origin="user_supplied",
        subject="A paragraph-backed lesson",
        question_ids=["learning-loop"],
        policy_revision="fixture-v1",
    )
    content = (
        "First supported paragraph.\n\nSecond supported paragraph.\n\n"
        "Third supported paragraph."
    )
    long_source = SourceRecord(
        source_id="source-0",
        candidate_id=candidate.candidate_id,
        origin="user_supplied",
        content=content,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        acquisition_status="ok",
        retrieved_at=datetime(2026, 9, 8, tzinfo=UTC),
        url="https://example.test/source-0",
    )

    bundle = prepare_evidence(candidate, [long_source], context_revision="fixture-v1")

    assert [p.passage_id for p in bundle.passages] == ["source-0:0", "source-0:1", "source-0:2"]
    assert [p.text for p in bundle.passages] == [
        "First supported paragraph.",
        "Second supported paragraph.",
        "Third supported paragraph.",
    ]
    assert "Only one attributable source was available." in bundle.coverage_gaps


def test_prepare_evidence_marks_a_single_unsplittable_body_as_coarse():
    candidate = Candidate(
        candidate_id="coarse-candidate",
        origin="user_supplied",
        subject="A single-line lesson",
        question_ids=["learning-loop"],
        policy_revision="fixture-v1",
    )
    content = "A single supported body without paragraph boundaries."
    single_line_source = SourceRecord(
        source_id="source-coarse",
        candidate_id=candidate.candidate_id,
        origin="user_supplied",
        content=content,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        acquisition_status="ok",
        retrieved_at=datetime(2026, 9, 8, tzinfo=UTC),
    )

    bundle = prepare_evidence(candidate, [single_line_source], context_revision="fixture-v1")

    assert bundle.passages[0].text == single_line_source.content
    assert "coarse passage" in bundle.coverage_gaps[0].lower()


def test_prepare_evidence_marks_one_non_empty_paragraph_with_blank_whitespace_as_coarse():
    candidate = Candidate(
        candidate_id="coarse-whitespace-candidate",
        origin="user_supplied",
        subject="A whitespace-padded lesson",
        question_ids=["learning-loop"],
        policy_revision="fixture-v1",
    )
    content = "  Only supported paragraph.  \n\n  "
    source = SourceRecord(
        source_id="source-coarse-whitespace",
        candidate_id=candidate.candidate_id,
        origin="user_supplied",
        content=content,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        acquisition_status="ok",
        retrieved_at=datetime(2026, 9, 8, tzinfo=UTC),
    )

    bundle = prepare_evidence(candidate, [source], context_revision="fixture-v1")

    assert len(bundle.passages) == 1
    assert "coarse passage" in bundle.coverage_gaps[0].lower()
