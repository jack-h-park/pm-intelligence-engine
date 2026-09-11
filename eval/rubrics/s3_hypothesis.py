"""S3 hypothesis quality rubric.

Checks that the Stage 3 hypothesis is:
  1. Non-empty
  2. Contains a testable claim (if/then or conditional structure)
  3. Does not begin with a feature description
"""

import re
from dataclasses import dataclass


@dataclass
class HypothesisCheckResult:
    passed: bool
    score: int  # 0–3
    issues: list[str]


_IF_THEN_PATTERNS = [
    re.compile(r"\bif\b.+\bthen\b", re.IGNORECASE),
    re.compile(r"\bif\b.+\bwill\b", re.IGNORECASE),
    re.compile(r"\bby\b.+\bwe (can|will|enable)\b", re.IGNORECASE),
    re.compile(r"\benabling\b.+\bwill\b", re.IGNORECASE),
]

_FEATURE_PREFIXES = re.compile(
    r"^(build|add|create|implement|ship|develop|launch)\b", re.IGNORECASE
)


def check(hypothesis: str) -> HypothesisCheckResult:
    issues: list[str] = []
    score = 0

    # Check 1: non-empty
    if not hypothesis or not hypothesis.strip():
        issues.append("Hypothesis is empty")
        return HypothesisCheckResult(passed=False, score=0, issues=issues)
    score += 1

    # Check 2: contains a testable conditional structure
    has_conditional = any(p.search(hypothesis) for p in _IF_THEN_PATTERNS)
    if not has_conditional:
        issues.append(
            "Hypothesis lacks a testable conditional structure (expected 'If X, then Y')"
        )
    else:
        score += 1

    # Check 3: does not start with a feature description
    stripped = hypothesis.strip()
    if _FEATURE_PREFIXES.match(stripped):
        issues.append(
            "Hypothesis appears to describe a feature, not a testable claim "
            "(starts with an action verb like 'Build', 'Add', 'Create')"
        )
    else:
        score += 1

    return HypothesisCheckResult(passed=len(issues) == 0, score=score, issues=issues)
