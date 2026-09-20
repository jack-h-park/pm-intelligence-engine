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
        1 for p in personas if any(kw.lower() in p.key_argument.lower() for kw in context_keywords)
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
                "Skeptic Quality: defaults to 'insufficient data' rather than a steelman counter-argument"  # noqa: E501
            )

    # --- 3. Open Question Quality (3 pts) ---
    actionable_count = sum(
        1 for p in personas if bool(_ACTIONABLE_QUESTION_PATTERNS.search(p.open_question))
    )
    if actionable_count == 4:
        open_question_quality = 3
    elif actionable_count >= 2:
        open_question_quality = 2
        issues.append(
            f"Open Question Quality: {4 - actionable_count} question(s) lack a named resolution method"  # noqa: E501
        )
    else:
        open_question_quality = 1
        issues.append(
            "Open Question Quality: most questions are abstract and don't name who answers or how"
        )

    # --- 4. Persona Independence (3 pts) ---
    # Score variance is not evidence of independent thought: a well-supported
    # consensus is valid. Instead require the intended lenses and distinct reasoning.
    expected_personas = {"explorer", "strategist", "builder", "skeptic"}
    present_personas = {p.persona for p in personas}
    normalized_arguments = {" ".join(p.key_argument.lower().split()) for p in personas}
    if present_personas == expected_personas and len(normalized_arguments) == len(personas):
        persona_independence = 3
    elif len(present_personas) >= 3 and len(normalized_arguments) >= 3:
        persona_independence = 2
        issues.append("Persona Independence: one persona lens or argument is not distinct")
    else:
        persona_independence = 1
        issues.append(
            "Persona Independence: all personas gave identical scores — evaluation lacks productive tension"  # noqa: E501
        )

    # Evidence_v1 adds explicit provenance and uncertainty checks. Legacy outputs
    # omit these fields and retain the historical four-dimension scoring behavior.
    evidence_v1 = any(
        p.evidence_passage_ids or p.option_assessments or p.option_positions or p.uncertainties
        for p in personas
    )
    evidence_linkage_quality = 0
    uncertainty_quality = 0
    if evidence_v1:
        evidence_linkage_quality = 3 if all(p.evidence_passage_ids for p in personas) else 1
        uncertainty_quality = 3 if all(p.uncertainties for p in personas) else 1
        if evidence_linkage_quality < 3:
            issues.append("Evidence Linkage: every persona must cite pinned case evidence")
        if uncertainty_quality < 3:
            issues.append("Uncertainty: every persona must state what would change its judgment")

    total = score_grounding + skeptic_quality + open_question_quality + persona_independence
    return S4RubricResult(
        total_score=total,
        score_grounding=score_grounding,
        skeptic_quality=skeptic_quality,
        open_question_quality=open_question_quality,
        persona_independence=persona_independence,
        evidence_linkage_quality=evidence_linkage_quality,
        uncertainty_quality=uncertainty_quality,
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
        "attack surface",
        "supply chain",
        "remote attestation",
        "kernel",
        "MDM",
        "AMAPI",
        "Knox",
        "APM",
        "MTD",
        "DISA",
        "STIG",
        "Pillar",
        "government",
        "defense",
        "enterprise",
        "KPE",
        "Android",
    ]
    for term in domain_terms:
        if term.lower() in product_context.lower():
            keywords.append(term)

    return list(set(keywords)) if keywords else ["strategy", "product", "user", "enterprise"]
