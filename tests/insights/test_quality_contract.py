"""Acceptance-report contracts for the separate semantic quality corpus."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.evaluate_insights import evaluate

CASE = {
    "case_id": "managed-profile-learning",
    "category": "practical_learning",
    "source": {
        "version": "fixture-v1",
        "text": "A policy now requires a managed profile.",
        "sha256": "ea758c8b491919d504a38b6bd23c740ac89f0154e53b5de330e03d1c20d5738e",
        "passages": [{"passage_id": "p1", "text": "A policy now requires a managed profile."}],
    },
    "historical": {"status": "not_preserved", "reason": "No versioned old run exists."},
    "expected": {
        "disposition": "ready",
        "facts": [{"text": "The policy requires a managed profile.", "passage_ids": ["p1"]}],
        "unknowns": ["Deployment coverage is not stated."],
        "usefulness": "Separate the stated policy from its unverified rollout.",
    },
}


def _evaluate(
    tmp_path: Path,
    result: dict | list[dict] | None,
    review: dict | None,
    case: dict | None = None,
) -> dict:
    cases_path = tmp_path / "cases.json"
    results_path = tmp_path / "results.json"
    reviews_path = tmp_path / "reviews.json"
    report_path = tmp_path / "report.json"
    cases_path.write_text(json.dumps({"schema_version": 1, "cases": [case or CASE]}))
    results_path.write_text(
        json.dumps({"results": result if isinstance(result, list) else [result] if result else []})
    )
    reviews_path.write_text(json.dumps({"reviews": [review] if review else []}))
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.evaluate_insights",
            "--cases",
            str(cases_path),
            "--results",
            str(results_path),
            "--reviews",
            str(reviews_path),
            "--output",
            str(report_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(report_path.read_text())


def _result(passage_id: str = "p1") -> dict:
    return {
        "case_id": CASE["case_id"],
        "mode": "same_evidence",
        "disposition": "ready",
        "claims": [{"text": "The policy requires a managed profile.", "passage_ids": [passage_id]}],
        "model": "fixture-provider",
        "prompt_revision": "fixture-prompt-v1",
        "source_cost_micros": 0,
        "llm_cost_micros": 0,
        "latency_ms": 4,
    }


def _review() -> dict:
    return {
        "case_id": CASE["case_id"],
        "mode": "same_evidence",
        "reviewer": "fixture-reviewer",
        "reviewer_kind": "human",
        "dimensions": {
            "explanation": {
                "verdict": "pass",
                "evidence": "p1 supports the explanation.",
                "passage_ids": ["p1"],
            },
            "freshness": {"verdict": "needs_evidence", "evidence": "Rollout date is absent."},
            "relevance": {
                "verdict": "pass",
                "evidence": "The managed-profile question is in scope.",
                "passage_ids": ["p1"],
            },
            "takeaway": {
                "verdict": "pass",
                "evidence": "The action remains bounded to validation.",
                "passage_ids": ["p1"],
            },
        },
    }


def test_report_keeps_quality_dimensions_and_cost_separate(tmp_path: Path) -> None:
    report = _evaluate(tmp_path, _result(), _review())
    row = report["cases"][0]

    assert row["source_integrity"] == "pass"
    assert row["disposition"] == {"expected": "ready", "actual": "ready", "status": "pass"}
    assert row["reference_integrity"] == "pass"
    assert row["dimensions"]["freshness"] == {
        "verdict": "needs_evidence",
        "evidence": "Rollout date is absent.",
    }
    assert row["dimensions"]["explanation"]["verdict"] == "pass"
    assert row["run_metadata"] == {
        "model": "fixture-provider",
        "prompt_revision": "fixture-prompt-v1",
        "source_cost_micros": 0,
        "llm_cost_micros": 0,
        "latency_ms": 4,
    }
    assert row["expected"]["facts"] == CASE["expected"]["facts"]
    assert row["expected"]["unknowns"] == CASE["expected"]["unknowns"]
    assert row["claims"] == _result()["claims"]
    assert row["reviewer"] == "fixture-reviewer"
    assert row["acceptance"] == "needs_review"


def test_unsupported_passage_fails_even_with_positive_review(tmp_path: Path) -> None:
    report = _evaluate(tmp_path, _result("invented"), _review())
    row = report["cases"][0]

    assert row["reference_integrity"] == "fail"
    assert row["unsupported_passage_ids"] == ["invented"]
    assert row["acceptance"] == "fail"


def test_missing_replay_and_review_stay_unknown(tmp_path: Path) -> None:
    report = _evaluate(tmp_path, None, None)
    row = report["cases"][0]

    assert row["historical"]["status"] == "not_preserved"
    assert row["disposition"]["status"] == "unavailable"
    assert row["reference_integrity"] == "unavailable"
    assert set(row["dimensions"].values()) == {"unreviewed"}
    assert row["acceptance"] == "needs_review"
    assert report["accepted_count"] == 0


def test_same_evidence_and_enriched_runs_are_reported_separately(tmp_path: Path) -> None:
    enriched = {
        **_result(),
        "mode": "enriched",
        "disposition": "needs_evidence",
        "model": "enrichment-fixture",
        "source_cost_micros": 11,
        "llm_cost_micros": 23,
        "latency_ms": 90,
    }
    row = _evaluate(tmp_path, [_result(), enriched], _review())["cases"][0]

    assert row["comparisons"]["same_evidence"]["disposition"] == "ready"
    assert row["comparisons"]["same_evidence"]["model"] == "fixture-provider"
    assert row["comparisons"]["enriched"] == {
        "disposition": "needs_evidence",
        "model": "enrichment-fixture",
        "prompt_revision": "fixture-prompt-v1",
        "source_cost_micros": 11,
        "llm_cost_micros": 23,
        "latency_ms": 90,
        "reference_integrity": "pass",
        "reviewer": None,
        "dimensions": {
            name: "unreviewed" for name in ("explanation", "freshness", "relevance", "takeaway")
        },
        "acceptance": "fail",
    }
    assert row["historical"]["status"] == "not_preserved"
    assert row["acceptance"] == "needs_review"


def test_changed_source_snapshot_fails_before_quality_review(tmp_path: Path) -> None:
    changed = {**CASE, "source": {**CASE["source"], "text": "Changed source body."}}
    row = _evaluate(tmp_path, _result(), _review(), changed)["cases"][0]

    assert row["source_integrity"] == "fail"
    assert row["acceptance"] == "fail"


def test_model_self_review_cannot_accept_a_case(tmp_path: Path) -> None:
    model_review = {**_review(), "reviewer_kind": "model"}
    model_review["dimensions"] = {
        name: {"verdict": "pass", "evidence": "Asserted by the model."}
        for name in ("explanation", "freshness", "relevance", "takeaway")
    }
    row = _evaluate(tmp_path, _result(), model_review)["cases"][0]

    assert set(row["dimensions"].values()) == {"unreviewed"}
    assert row["acceptance"] == "needs_review"


def test_unknown_cost_cannot_be_an_accepted_replay(tmp_path: Path) -> None:
    passing_review = _review()
    passing_review["dimensions"]["freshness"] = {
        "verdict": "pass",
        "evidence": "The reviewer considered the stated unknown.",
    }
    result = _result()
    result["llm_cost_micros"] = None
    row = _evaluate(tmp_path, result, passing_review)["cases"][0]

    assert row["acceptance"] == "needs_review"


def test_enriched_output_cannot_hide_an_unsupported_reference(tmp_path: Path) -> None:
    enriched = {**_result("invented"), "mode": "enriched"}
    row = _evaluate(tmp_path, [_result(), enriched], _review())["cases"][0]

    assert row["comparisons"]["enriched"]["reference_integrity"] == "fail"
    assert row["comparisons"]["enriched"]["acceptance"] == "fail"
    assert row["acceptance"] == "needs_review"


def test_enriched_review_has_its_own_quality_verdict(tmp_path: Path) -> None:
    same_review = _review()
    enriched_review = {**_review(), "mode": "enriched"}
    enriched_review["dimensions"] = {
        name: {"verdict": "pass", "evidence": "Supported by p1.", "passage_ids": ["p1"]}
        for name in ("explanation", "freshness", "relevance", "takeaway")
    }
    report = evaluate(
        [CASE], [_result(), {**_result(), "mode": "enriched"}], [same_review, enriched_review]
    )
    row = report["cases"][0]
    assert row["acceptance"] == "needs_review"
    assert row["comparisons"]["enriched"]["acceptance"] == "pass"
    assert row["comparisons"]["enriched"]["reviewer"] == "fixture-reviewer"


def test_positive_review_requires_a_supported_passage(tmp_path: Path) -> None:
    review = _review()
    review["dimensions"]["freshness"] = {
        "verdict": "pass",
        "evidence": "Current according to reviewer.",
        "passage_ids": ["invented"],
    }
    row = _evaluate(tmp_path, _result(), review)["cases"][0]
    assert row["dimensions"]["freshness"] == "unreviewed"
    assert row["acceptance"] == "needs_review"


def test_enriched_reference_requires_a_hashed_additional_source(tmp_path: Path) -> None:
    source_text = "A second source confirms a managed-profile rollout in the pilot fleet."
    enriched = {
        **_result("p2"),
        "mode": "enriched",
        "enriched_sources": [
            {
                "version": "fixture-enrichment-v1",
                "text": source_text,
                "sha256": "c1f6900bd1f5c844aed1c36ece302b3c3fc1df2a4fdee9d89d00baf14e3bd615",
                "passages": [{"passage_id": "p2", "text": source_text}],
            }
        ],
    }
    row = _evaluate(tmp_path, [_result(), enriched], _review())["cases"][0]

    assert row["comparisons"]["enriched"]["reference_integrity"] == "pass"


def test_expected_fact_must_point_to_a_frozen_passage(tmp_path: Path) -> None:
    changed = {
        **CASE,
        "expected": {
            **CASE["expected"],
            "facts": [{"text": "A policy changed.", "passage_ids": ["invented"]}],
        },
    }
    row = _evaluate(tmp_path, _result(), _review(), changed)["cases"][0]

    assert row["source_integrity"] == "fail"
    assert row["acceptance"] == "fail"


def test_ready_result_needs_a_claim_with_a_frozen_passage(tmp_path: Path) -> None:
    result = {**_result(), "claims": []}
    review = _review()
    review["dimensions"]["freshness"] = {
        "verdict": "pass",
        "evidence": "The dated source supports this.",
        "passage_ids": ["p1"],
    }
    row = _evaluate(tmp_path, result, review)["cases"][0]
    assert row["reference_integrity"] == "fail"
    assert row["acceptance"] == "fail"


def test_source_version_is_required(tmp_path: Path) -> None:
    changed = {**CASE, "source": {**CASE["source"], "version": ""}}
    row = _evaluate(tmp_path, _result(), _review(), changed)["cases"][0]
    assert row["source_integrity"] == "fail"


def test_unmatched_replay_cannot_silently_disappear_from_report() -> None:
    with pytest.raises(ValueError, match="unknown case"):
        evaluate([CASE], [{**_result(), "case_id": "unmatched"}], [])


def test_frozen_quality_corpus_has_twelve_distinct_review_scenarios(tmp_path: Path) -> None:
    fixture_path = (
        Path(__file__).parents[1] / "fixtures" / "signal_intelligence" / "quality_cases.json"
    )
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    categories = {case["category"] for case in payload["cases"]}
    assert categories == {
        "practical_learning",
        "commercial_judgment",
        "platform_mechanism",
        "strong_historical_output",
        "weak_historical_output",
        "late_body_evidence",
        "thin_source",
        "old_useful_background",
        "superseded_release",
        "repeat_with_delta",
        "repeat_without_delta",
        "no_agent_handoff",
    }
    assert len(payload["cases"]) == 12
    assert all(
        case["expected"]["facts"]
        and case["expected"]["unknowns"]
        and case["expected"]["usefulness"]
        for case in payload["cases"]
    )

    results_path = tmp_path / "results.json"
    reviews_path = tmp_path / "reviews.json"
    report_path = tmp_path / "report.json"
    results_path.write_text('{"results": []}')
    reviews_path.write_text('{"reviews": []}')
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.evaluate_insights",
            "--cases",
            str(fixture_path),
            "--results",
            str(results_path),
            "--reviews",
            str(reviews_path),
            "--output",
            str(report_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(report_path.read_text())
    assert report["case_count"] == 12
    assert report["accepted_count"] == 0
    assert all(row["source_integrity"] == "pass" for row in report["cases"])
    assert all(row["acceptance"] == "needs_review" for row in report["cases"])

    by_category = {case["category"]: case for case in payload["cases"]}
    for category in ("late_body_evidence", "weak_historical_output", "repeat_with_delta"):
        case = by_category[category]
        assert len(case["source"]["passages"]) >= 2
        assert case["expected"]["facts"][0]["passage_ids"] == [
            case["source"]["passages"][-1]["passage_id"]
        ]
