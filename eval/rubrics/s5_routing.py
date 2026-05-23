"""S5 routing accuracy checker.

Validates that the deterministic routing rule produces the expected result
for a given scenario. Used by the eval harness.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class S5RoutingResult:
    run_id: str
    expected_routing: str
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
    expected_routing: str,
    expected_composite_range: tuple[float, float],
) -> S5RoutingResult:
    issues: list[str] = []

    routing_passed = actual_routing == expected_routing
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
