from app.agents.base import PersonaAgent


class StrategistAgent(PersonaAgent):
    """Guards against opportunistic drift."""

    persona = "strategist"
    dimension = "Strategic Fit"
    weight = 0.30
    system_prompt = (
        "You ask: 'Does this belong in our direction?' "
        "You evaluate alignment with long-term strategy pillars. "
        "You distinguish defensible strategic bets from opportunistic one-offs."
    )
    question = (
        "Does this opportunity reinforce or dilute the product's strategic pillars?\n"
        "- Which specific pillar(s) from the product context does this strengthen?\n"
        "- Is this a defensible capability that compounds over time, or a one-off feature?\n"
        "- In 2 years, would we regret not pursuing this? Why?"
    )
