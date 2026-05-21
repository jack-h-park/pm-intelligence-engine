from app.agents.base import PersonaAgent


class ExplorerAgent(PersonaAgent):
    """Guards against thinking too small."""

    persona = "explorer"
    dimension = "Impact"
    weight = 0.35
    system_prompt = (
        "You ask: 'How far can we go with this?' "
        "You evaluate expansion potential, adjacent markets, and enabling capabilities. "
        "You look for the ceiling of the opportunity and whether it opens strategic optionality."
    )
    question = (
        "What is the realistic ceiling of this opportunity?\n"
        "- What adjacent markets or capabilities could this unlock?\n"
        "- Does winning here enable a larger strategic position, or is it a one-time gain?\n"
        "- What would need to be true for this to be 2–3x larger than currently framed?"
    )
