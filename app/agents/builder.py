from app.agents.base import PersonaAgent


class BuilderAgent(PersonaAgent):
    """Guards against ideation without grounding.

    Lens and evaluation question live in
    pm-decision-context/prompts/s4-personas/builder.md (loaded at runtime).
    """

    persona = "builder"
    dimension = "Feasibility"
    weight = 0.20
