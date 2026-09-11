"""Unit tests for TemplateService persona-prompt loading (US-37).

The persona lens/question text moved from app/agents/*.py into
pm-decision-context/prompts/s4-personas/*.md. These tests pin the parsed text
to the exact strings the engine sent before the migration, so the move is
behavior-preserving (eval stays calibrated). They read the real
decision-context files via the configured DECISION_SYSTEM_ROOT.
"""

import pytest

from app.services.template_service import TemplateService, _parse_sections
from config import settings

# Canonical persona lens/question text. Originally the strings hardcoded as
# PersonaAgent.system_prompt/.question (US-37 behavior-preservation); the Explorer
# question gained a reach-quantification first bullet in US-32. These pin the
# parser output to the authored decision-context text.
_EXPECTED = {
    "explorer": {
        "lens": (
            "You ask: 'How far can we go with this?' "
            "You evaluate expansion potential, adjacent markets, and enabling capabilities. "
            "You look for the ceiling of the opportunity and whether it opens strategic optionality."  # noqa: E501
        ),
        "question": (
            "What is the current reach and the realistic ceiling of this opportunity?\n"
            "- Current footprint: which user segments, and at what scale, does this affect today?\n"
            "- What adjacent markets or capabilities could this unlock?\n"
            "- Does winning here enable a larger strategic position, or is it a one-time gain?\n"
            "- What would need to be true for this to be 2–3x larger than currently framed?"
        ),
    },
    "strategist": {
        "lens": (
            "You ask: 'Does this belong in our direction?' "
            "You evaluate alignment with long-term strategy pillars. "
            "You distinguish defensible strategic bets from opportunistic one-offs."
        ),
        "question": (
            "Does this opportunity reinforce or dilute the product's strategic pillars?\n"
            "- Which specific pillar(s) from the product context does this strengthen?\n"
            "- Is this a defensible capability that compounds over time, or a one-off feature?\n"
            "- In 2 years, would we regret not pursuing this? Why?"
        ),
    },
    "builder": {
        "lens": (
            "You ask: 'Can we actually make this?' "
            "You evaluate execution feasibility given current team, APIs, and constraints. "
            "You find the highest-risk technical or organizational assumption."
        ),
        "question": (
            "What would it take to build this, and what is the highest-risk execution assumption?\n"
            "- What is the minimum team size and time to ship a v1?\n"
            "- What external dependencies (API access, partner agreements, platform support) are required?\n"  # noqa: E501
            "- What is the single assumption about execution that, if wrong, would block delivery entirely?"  # noqa: E501
        ),
    },
    "skeptic": {
        "lens": (
            "You ask: 'What if we're wrong about this?' "
            "You challenge assumptions by constructing the strongest possible counter-argument. "
            "You do NOT default to 'insufficient data' — you reason from available evidence to find disconfirming signals."  # noqa: E501
        ),
        "question": (
            "What is the strongest argument AGAINST pursuing this opportunity?\n"
            "- Steelman the counter-argument: what assumption, if false, makes this opportunity worthless?\n"  # noqa: E501
            "- Is there existing evidence (from the product context or signal) that contradicts the hypothesis?\n"  # noqa: E501
            "- What would change your confidence score from its current level to 1?"
        ),
    },
}


@pytest.fixture()
def service() -> TemplateService:
    return TemplateService(settings.decision_system_root)


@pytest.mark.parametrize("persona", sorted(_EXPECTED))
def test_persona_prompt_matches_pre_migration_text(service, persona):
    loaded = service.load_persona_prompt(persona)
    assert loaded["lens"] == _EXPECTED[persona]["lens"]
    assert loaded["question"] == _EXPECTED[persona]["question"]


def test_load_persona_prompt_unknown_persona_raises(service):
    with pytest.raises(FileNotFoundError):
        service.load_persona_prompt("nonexistent")


def test_parse_sections_basic():
    text = "# Title\n\n## Lens\nlens body\n\n## Evaluation Question\nq line 1\n- bullet\n"
    sections = _parse_sections(text)
    assert sections["lens"] == "lens body"
    assert sections["evaluation question"] == "q line 1\n- bullet"


def test_validate_persona_prompts_passes_with_real_files(service):
    # All four persona files exist in decision-context — no raise.
    service.validate_persona_prompts(["explorer", "strategist", "builder", "skeptic"])


def test_validate_persona_prompts_fails_fast_when_missing(tmp_path):
    # Simulate a stale decision-context checkout without persona files.
    bad = TemplateService(str(tmp_path))
    with pytest.raises(RuntimeError) as exc:
        bad.validate_persona_prompts(["explorer", "skeptic"])
    msg = str(exc.value)
    assert "explorer" in msg and "skeptic" in msg
    assert "deploy decision-context" in msg


def test_parse_sections_ignores_html_comment_and_h1():
    text = "<!-- comment -->\n# Heading\nintro\n## Lens\nbody\n"
    sections = _parse_sections(text)
    assert sections["lens"] == "body"
    # Content before the first ## heading is not captured as a section
    assert "lens" in sections and len(sections) == 1
