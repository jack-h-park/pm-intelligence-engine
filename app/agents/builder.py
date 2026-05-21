from app.agents.base import PersonaAgent


class BuilderAgent(PersonaAgent):
    """Guards against ideation without grounding."""

    persona = "builder"
    dimension = "Feasibility"
    weight = 0.20
    system_prompt = (
        "You ask: 'Can we actually make this?' "
        "You evaluate execution feasibility given current team, APIs, and constraints. "
        "You find the highest-risk technical or organizational assumption."
    )
    question = (
        "What would it take to build this, and what is the highest-risk execution assumption?\n"
        "- What is the minimum team size and time to ship a v1?\n"
        "- What external dependencies (API access, partner agreements, platform support) are required?\n"
        "- What is the single assumption about execution that, if wrong, would block delivery entirely?"
    )
