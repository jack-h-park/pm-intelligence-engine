"""S5 routing accuracy checker.

Validates that the deterministic routing rule produces the expected result
for a given scenario. Used by the eval harness.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class S5RoutingResult:
    run_id: str
    expected_routing: str | list[str]
    actual_routing: str
    composite_score: float
    expected_composite_range: tuple[float, float]
    routing_passed: bool
    composite_passed: bool
    passed: bool
    issues: list[str]


def check(
    run_id: str,
    actual_routing: str,
    actual_composite: float,
    expected_routing: str | list[str],
    expected_composite_range: tuple[float, float],
) -> S5RoutingResult:
    issues: list[str] = []

    # A scenario may accept multiple routings when the deterministic rule's
    # outcome legitimately depends on a persona score that varies across
    # models/runs (e.g. prd vs poc on the Confidence gate). A single string
    # remains a strict expectation.
    accepted = [expected_routing] if isinstance(expected_routing, str) else expected_routing
    routing_passed = actual_routing in accepted
    if not routing_passed:
        issues.append(
            f"Routing mismatch: expected '{expected_routing}', got '{actual_routing}'"
        )

    lo, hi = expected_composite_range
    composite_passed = lo <= actual_composite <= hi
    if not composite_passed:
        issues.append(
            f"Composite out of range: expected [{lo}, {hi}], got {actual_composite}"
        )

    return S5RoutingResult(
        run_id=run_id,
        expected_routing=expected_routing,
        actual_routing=actual_routing,
        composite_score=actual_composite,
        expected_composite_range=expected_composite_range,
        routing_passed=routing_passed,
        composite_passed=composite_passed,
        passed=routing_passed,  # composite range is informational only
        issues=issues,
    )
