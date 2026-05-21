"""S4 evaluation quality rubric — 12-point checker.

Dimensions (3 pts each):
  1. Score Grounding: personas reference specific product context elements
  2. Skeptic Quality: counter-argument is substantive, not "insufficient data"
  3. Open Question Quality: questions name who answers and how
  4. Persona Independence: scores and arguments show productive differentiation

Passing threshold: 9/12.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.models.stages import PersonaOutput, S4RubricResult

_DATA_GAP_PATTERNS = re.compile(
    r"\b(insufficient data|lack(ing)? (data|evidence|information)|no data|"
    r"we don't have|not enough (data|evidence)|unclear without|"
    r"cannot determine|unable to assess)\b",
    re.IGNORECASE,
)

_ACTIONABLE_QUESTION_PATTERNS = re.compile(
    r"\b(interview|survey|spike|prototype|pilot|legal review|eng(ineering)? review|"
    r"customer (call|feedback|research)|market research|data analysis|"
    r"A/B test|usability test|competitive analysis|partner (meeting|discussion))\b",
    re.IGNORECASE,
)


def check(personas: list[PersonaOutput], product_context: str) -> S4RubricResult:
    issues: list[str] = []

    # --- 1. Score Grounding (3 pts) ---
    # Extract key context terms from product context (pillars, constraints, pain points)
    context_keywords = _extract_context_keywords(product_context)
    grounded_count = sum(
        1
        for p in personas
        if any(kw.lower() in p.key_argument.lower() for kw in context_keywords)
    )
    if grounded_count == 4:
        score_grounding = 3
    elif grounded_count == 3:
        score_grounding = 2
        issues.append("Score Grounding: 1 persona did not reference specific product context")
    else:
        score_grounding = 1
        issues.append(
            f"Score Grounding: only {grounded_count}/4 personas reference specific product context"
        )

    # --- 2. Skeptic Quality (3 pts) ---
    skeptic = next((p for p in personas if p.persona == "skeptic"), None)
    if skeptic is None:
        skeptic_quality = 1
        issues.append("Skeptic Quality: no skeptic output found")
    else:
        has_data_gap = bool(_DATA_GAP_PATTERNS.search(skeptic.key_argument))
        arg_length = len(skeptic.key_argument.split())
        if not has_data_gap and arg_length >= 30:
            skeptic_quality = 3
        elif not has_data_gap:
            skeptic_quality = 2
            issues.append("Skeptic Quality: counter-argument is too brief")
        else:
            skeptic_quality = 1
            issues.append(
                "Skeptic Quality: defaults to 'insufficient data' rather than a steelman counter-argument"
            )

    # --- 3. Open Question Quality (3 pts) ---
    actionable_count = sum(
        1
        for p in personas
        if bool(_ACTIONABLE_QUESTION_PATTERNS.search(p.open_question))
    )
    if actionable_count == 4:
        open_question_quality = 3
    elif actionable_count >= 2:
        open_question_quality = 2
        issues.append(
            f"Open Question Quality: {4 - actionable_count} question(s) lack a named resolution method"
        )
    else:
        open_question_quality = 1
        issues.append(
            "Open Question Quality: most questions are abstract and don't name who answers or how"
        )

    # --- 4. Persona Independence (3 pts) ---
    scores = [p.score for p in personas]
    skeptic_score = next((p.score for p in personas if p.persona == "skeptic"), None)
    non_skeptic_scores = [p.score for p in personas if p.persona != "skeptic"]
    score_variance = max(scores) - min(scores)

    skeptic_differs = skeptic_score is not None and skeptic_score != sum(non_skeptic_scores) / len(
        non_skeptic_scores
    )
    if score_variance >= 2 and skeptic_differs:
        persona_independence = 3
    elif score_variance >= 1:
        persona_independence = 2
        if not skeptic_differs:
            issues.append(
                "Persona Independence: Skeptic score matches the average of other personas"
            )
    else:
        persona_independence = 1
        issues.append(
            "Persona Independence: all personas gave identical scores — evaluation lacks productive tension"
        )

    total = score_grounding + skeptic_quality + open_question_quality + persona_independence
    return S4RubricResult(
        total_score=total,
        score_grounding=score_grounding,
        skeptic_quality=skeptic_quality,
        open_question_quality=open_question_quality,
        persona_independence=persona_independence,
        passed=total >= 9,
        issues=issues,
    )


def _extract_context_keywords(product_context: str) -> list[str]:
    """Extract meaningful keywords from product context for grounding check."""
    keywords: list[str] = []

    # Extract pillar names (lines starting with a number or bullet near "Pillar")
    for line in product_context.splitlines():
        stripped = line.strip()
        # Strategy pillar lines typically start with number + period or bold marker
        if re.match(r"^\d+\.\s+\*\*", stripped) or re.match(r"^[-*]\s+\*\*", stripped):
            # Extract the bold text as keyword
            match = re.search(r"\*\*([^*]+)\*\*", stripped)
            if match:
                keywords.append(match.group(1).split(":")[0].strip())

    # Add generic high-value domain terms that appear in context
    domain_terms = [
        "attack surface", "supply chain", "remote attestation", "kernel",
        "MDM", "AMAPI", "Knox", "APM", "MTD", "DISA", "STIG", "Pillar",
        "government", "defense", "enterprise", "KPE", "Android",
    ]
    for term in domain_terms:
        if term.lower() in product_context.lower():
            keywords.append(term)

    return list(set(keywords)) if keywords else ["strategy", "product", "user", "enterprise"]
