import json
from pathlib import Path


def test_s2k_calibration_corpus_covers_the_required_semantic_cases() -> None:
    fixture_path = Path(__file__).parents[1] / "fixtures" / "signal_intelligence" / "cases.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))

    cases = payload["cases"]
    expected_ids = {
        "actionable-learning-no-product",
        "android-enterprise-strong-connection",
        "security-product-strong-connection",
        "ambiguous-product-connection",
        "unsupported-keyword-overlap",
        "weak-fallback-evidence",
        "duplicate-content",
        "stale-background-context",
        "late-body-title-only",
        "superseded-evidence",
        "repeated-no-material-delta",
        "missing-provenance",
    }

    assert payload["fixture_version"] == 2
    assert {case["case_id"] for case in cases} == expected_ids
    for case in cases:
        expected = case["expected"]
        assert expected["s2k_disposition"] in {"ready", "needs_evidence", "no_new_learning"}
        assert expected["connection_assessment"] in {
            "candidates",
            "ambiguous",
            "no_clear_connection",
        }
        assert isinstance(expected["candidate_product_ids"], list)
        assert isinstance(expected["required_passage_ids"], list)
        assert case["candidate"]["policy_revision"] == "fixture-v2"
