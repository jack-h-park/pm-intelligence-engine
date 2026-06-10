from app.agents.base import PersonaAgent


class StrategistAgent(PersonaAgent):
    """Guards against opportunistic drift.

    Lens and evaluation question live in
    pm-decision-context/prompts/s4-personas/strategist.md (loaded at runtime).
    """

    persona = "strategist"
    dimension = "Strategic Fit"
    weight = 0.30
