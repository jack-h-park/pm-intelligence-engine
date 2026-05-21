from app.agents.base import PersonaAgent


class SkepticAgent(PersonaAgent):
    """Guards against confirmation bias."""

    persona = "skeptic"
    dimension = "Confidence"
    weight = 0.15
    system_prompt = (
        "You ask: 'What if we're wrong about this?' "
        "You challenge assumptions by constructing the strongest possible counter-argument. "
        "You do NOT default to 'insufficient data' — you reason from available evidence to find disconfirming signals."
    )
    question = (
        "What is the strongest argument AGAINST pursuing this opportunity?\n"
        "- Steelman the counter-argument: what assumption, if false, makes this opportunity worthless?\n"
        "- Is there existing evidence (from the product context or signal) that contradicts the hypothesis?\n"
        "- What would change your confidence score from its current level to 1?"
    )
