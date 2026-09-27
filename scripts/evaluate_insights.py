"""Compare frozen Insight cases without making a provider call or hiding review gaps."""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

DIMENSIONS = ("explanation", "freshness", "relevance", "takeaway")


def _index(rows: list[dict[str, Any]], label: str) -> dict[tuple[str, str], dict[str, Any]]:
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["case_id"], row.get("mode", "same_evidence"))
        if key in indexed:
            raise ValueError(f"duplicate {label} for {key[0]} / {key[1]}")
        indexed[key] = row
    return indexed


def _snapshot_integrity(source: dict[str, Any]) -> str:
    if not source.get("version"):
        return "fail"
    text = source["text"]
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != source["sha256"]:
        return "fail"
    passages = source["passages"]
    identifiers = [passage["passage_id"] for passage in passages]
    if len(identifiers) != len(set(identifiers)):
        return "fail"
    if any(not passage["text"] or passage["text"] not in text for passage in passages):
        return "fail"
    return "pass"


def _reference_status(
    case: dict[str, Any],
    run: dict[str, Any] | None,
    mode: str,
) -> tuple[str, list[str]]:
    if run is None:
        return "unavailable", []
    sources = [case["source"]]
    additional = run.get("enriched_sources", [])
    if mode == "same_evidence" and additional:
        return "fail", []
    if mode == "enriched":
        sources.extend(additional)
    if any(_snapshot_integrity(source) == "fail" for source in sources):
        return "fail", []
    identifiers = [passage["passage_id"] for source in sources for passage in source["passages"]]
    if len(identifiers) != len(set(identifiers)):
        return "fail", []
    allowed = set(identifiers)
    claims = run.get("claims", [])
    if run.get("disposition") == "ready" and not claims:
        return "fail", []
    if any(not claim.get("text") or not claim.get("passage_ids") for claim in claims):
        return "fail", []
    unsupported = sorted(
        {
            passage_id
            for claim in claims
            for passage_id in claim.get("passage_ids", [])
            if passage_id not in allowed
        }
    )
    return ("fail" if unsupported else "pass"), unsupported


def _metadata_complete(run: dict[str, Any] | None) -> bool:
    if run is None or not run.get("model") or not run.get("prompt_revision"):
        return False
    return all(
        isinstance(run.get(key), int) and run[key] >= 0
        for key in ("source_cost_micros", "llm_cost_micros", "latency_ms")
    )


def _review_dimensions(review: dict[str, Any] | None, allowed: set[str]) -> dict[str, Any]:
    if review is None or review.get("reviewer_kind") != "human" or not review.get("reviewer"):
        return {name: "unreviewed" for name in DIMENSIONS}
    dimensions = review.get("dimensions", {})
    output: dict[str, Any] = {}
    for name in DIMENSIONS:
        item = dimensions.get(name)
        if (
            not isinstance(item, dict)
            or item.get("verdict") not in {"pass", "fail", "needs_evidence"}
            or not str(item.get("evidence", "")).strip()
            or (
                item.get("verdict") == "pass"
                and (not item.get("passage_ids") or not set(item["passage_ids"]) <= allowed)
            )
        ):
            output[name] = "unreviewed"
        else:
            output[name] = {
                key: item[key] for key in ("verdict", "evidence", "passage_ids") if key in item
            }
    return output


def _acceptance(
    source_integrity: str,
    disposition_status: str,
    reference_integrity: str,
    dimensions: dict[str, Any],
    run: dict[str, Any] | None,
) -> str:
    if (
        source_integrity == "fail"
        or disposition_status == "fail"
        or reference_integrity == "fail"
        or any(
            isinstance(value, dict) and value["verdict"] == "fail" for value in dimensions.values()
        )
    ):
        return "fail"
    if (
        disposition_status == "pass"
        and reference_integrity == "pass"
        and all(
            isinstance(value, dict) and value["verdict"] == "pass" for value in dimensions.values()
        )
        and _metadata_complete(run)
    ):
        return "pass"
    return "needs_review"


def evaluate(
    cases: list[dict[str, Any]],
    results: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
) -> dict[str, Any]:
    result_index = _index(results, "result")
    review_index = _index(reviews, "review")
    case_ids = {case["case_id"] for case in cases}
    if len(case_ids) != len(cases):
        raise ValueError("duplicate quality case ID")
    for key in (*result_index, *review_index):
        if key[0] not in case_ids:
            raise ValueError(f"unknown case in evaluation input: {key[0]}")
        if key[1] not in {"same_evidence", "enriched"}:
            raise ValueError(f"unsupported comparison mode: {key[1]}")
    rows = []
    for case in cases:
        case_id = case["case_id"]
        run = result_index.get((case_id, "same_evidence"))
        review = review_index.get((case_id, "same_evidence"))
        integrity = _snapshot_integrity(case["source"])
        allowed_facts = {passage["passage_id"] for passage in case["source"]["passages"]}
        if any(
            not fact.get("text")
            or not fact.get("passage_ids")
            or not set(fact["passage_ids"]) <= allowed_facts
            for fact in case["expected"]["facts"]
        ):
            integrity = "fail"
        expected = case["expected"]["disposition"]
        disposition = {
            "expected": expected,
            "actual": run["disposition"] if run is not None else None,
            "status": (
                "unavailable"
                if run is None
                else "pass"
                if run["disposition"] == expected
                else "fail"
            ),
        }
        reference_integrity, unsupported = _reference_status(case, run, "same_evidence")
        dimensions = _review_dimensions(review, allowed_facts)
        acceptance = _acceptance(
            integrity, disposition["status"], reference_integrity, dimensions, run
        )
        metadata = (
            None
            if run is None
            else {
                key: run.get(key)
                for key in (
                    "model",
                    "prompt_revision",
                    "source_cost_micros",
                    "llm_cost_micros",
                    "latency_ms",
                )
            }
        )
        comparisons: dict[str, dict[str, Any] | None] = {}
        for mode in ("same_evidence", "enriched"):
            candidate = result_index.get((case_id, mode))
            if candidate is None:
                comparisons[mode] = None
                continue
            status, _ = _reference_status(case, candidate, mode)
            allowed = allowed_facts | {
                passage["passage_id"]
                for source in candidate.get("enriched_sources", [])
                for passage in source["passages"]
            }
            mode_dimensions = _review_dimensions(review_index.get((case_id, mode)), allowed)
            comparison = {
                key: candidate.get(key)
                for key in (
                    "disposition",
                    "model",
                    "prompt_revision",
                    "source_cost_micros",
                    "llm_cost_micros",
                    "latency_ms",
                )
            }
            comparison["reference_integrity"] = status
            mode_review = review_index.get((case_id, mode))
            comparison["reviewer"] = (
                mode_review.get("reviewer")
                if mode_review and mode_review.get("reviewer_kind") == "human"
                else None
            )
            comparison["dimensions"] = mode_dimensions
            comparison["acceptance"] = _acceptance(
                integrity,
                "pass" if candidate["disposition"] == expected else "fail",
                status,
                mode_dimensions,
                candidate,
            )
            comparisons[mode] = comparison
        rows.append(
            {
                "case_id": case_id,
                "category": case["category"],
                "expected": case["expected"],
                "source_integrity": integrity,
                "historical": case["historical"],
                "disposition": disposition,
                "reference_integrity": reference_integrity,
                "unsupported_passage_ids": unsupported,
                "claims": run.get("claims", []) if run is not None else None,
                "reviewer": review["reviewer"]
                if review and review.get("reviewer_kind") == "human"
                else None,
                "dimensions": dimensions,
                "run_metadata": metadata,
                "comparisons": comparisons,
                "acceptance": acceptance,
            }
        )
    return {
        "case_count": len(rows),
        "accepted_count": sum(row["acceptance"] == "pass" for row in rows),
        "failed_count": sum(row["acceptance"] == "fail" for row in rows),
        "cases": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--reviews", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))["cases"]
    results = json.loads(args.results.read_text(encoding="utf-8"))["results"]
    reviews = json.loads(args.reviews.read_text(encoding="utf-8"))["reviews"]
    report = evaluate(cases, results, reviews)
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
