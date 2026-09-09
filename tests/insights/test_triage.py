import json

import pytest

from app.services.insight_triage import triage_source


class FixtureLLM:
    def __init__(self, payload):
        self.payload = payload

    async def complete(self, messages, **kwargs):
        return json.dumps(self.payload)


@pytest.mark.asyncio
async def test_triage_admits_relevant_evidence_with_a_meaningful_delta():
    result = await triage_source(
        question="What practical learning should be tested next?",
        title="A change",
        content="Evidence.",
        llm=FixtureLLM({
            "disposition": "admit", "relevance": "relevant",
            "novelty": "meaningful_delta", "reason": "New attributed mechanism.",
        }),
    )

    assert result.disposition == "admit"


@pytest.mark.asyncio
async def test_triage_never_admits_unchanged_or_irrelevant_evidence():
    result = await triage_source(
        question="What practical learning should be tested next?",
        title="An old item",
        content="Evidence.",
        llm=FixtureLLM({
            "disposition": "admit", "relevance": "irrelevant",
            "novelty": "unchanged", "reason": "No delta.",
        }),
    )

    assert result.disposition == "quiet_reference"
