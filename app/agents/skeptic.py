from app.agents.base import PersonaAgent


class SkepticAgent(PersonaAgent):
    """Guards against confirmation bias.

    Lens and evaluation question live in
    pm-decision-context/prompts/s4-personas/skeptic.md (loaded at runtime).
    """

    persona = "skeptic"
    dimension = "Confidence"
    weight = 0.15
