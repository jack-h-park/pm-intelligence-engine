from app.agents.base import PersonaAgent


class ExplorerAgent(PersonaAgent):
    """Guards against thinking too small.

    Lens and evaluation question live in
    pm-decision-context/prompts/s4-personas/explorer.md (loaded at runtime).
    """

    persona = "explorer"
    dimension = "Impact"
    weight = 0.35
