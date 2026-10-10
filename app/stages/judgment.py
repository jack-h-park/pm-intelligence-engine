"""The judgment-pack block appended to a stage's system message.

Only the stages that make the judgment call carry it: S3 frames the opportunity
and S5 decides its route. Earlier stages summarise a signal and later ones write
up a decision already taken, so the pack would cost tokens there without
changing anything.
"""

from app.models.stages import RunContext


def judgment_patterns_block(context: RunContext) -> str:
    """The pack under its own heading, or "" when the run has none."""
    if not context.judgment_patterns:
        return ""
    return (
        "\n\n---\n\n## Judgment Patterns\n\n"
        "Reusable decision patterns compiled from the PM knowledge base. Apply a "
        "pattern only where it bears on this decision, and say which one you applied.\n\n"
        f"{context.judgment_patterns}"
    )
