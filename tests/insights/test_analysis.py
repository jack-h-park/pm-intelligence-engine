import json

import pytest

from app.models.insights import EvidenceBundle, Passage
from app.services.insight_analysis import analyze_bundle
from app.services.insight_context import PreparedContext


class FixtureLLM:
    async def complete(self, messages, **kwargs):
        return json.dumps(
            {
                "headline": "A practical learning signal",
                "explanation": "The supplied evidence supports a bounded learning observation.",
                "actual_change": "The reported practice is now explicit.",
                "why_now": "The candidate was newly submitted.",
                "personal_relevance": "It informs the stated learning question.",
                "takeaway": "Test the practice in a small, observable setting.",
                "claims": [
                    {
                        "text": "The practice was explicitly reported.",
                        "passage_ids": ["passage-learning-1"],
                    }
                ],
                "uncertainties": ["No independent replication was supplied."],
            }
        )


@pytest.fixture()
def learning_bundle():
    return EvidenceBundle(
        bundle_id="bundle-learning",
        candidate_id="candidate-learning",
        source_ids=["source-learning"],
        passages=[
            Passage(
                passage_id="passage-learning-1",
                source_id="source-learning",
                locator="fixture",
                text="The source reports a practical learning observation.",
                role="seed",
            )
        ],
        freshness_status="unknown",
        context_revision="fixture-v1",
    )


@pytest.fixture()
def context():
    return PreparedContext(
        candidate_id="candidate-learning",
        bundle_id="bundle-learning",
        question="What should I learn from this?",
        context_revision="fixture-v1",
        validation_status="valid",
    )


@pytest.mark.asyncio
async def test_learning_does_not_require_product_or_note(learning_bundle, context):
    result = await analyze_bundle(learning_bundle, context, FixtureLLM())

    assert result.takeaway
    assert result.note_connections == []
    assert set(result.claims[0].passage_ids) <= learning_bundle.passage_ids


@pytest.mark.asyncio
async def test_analysis_rejects_claims_without_bundle_passages(learning_bundle, context):
    class BadCitationLLM(FixtureLLM):
        async def complete(self, messages, **kwargs):
            response = json.loads(await super().complete(messages, **kwargs))
            response["claims"][0]["passage_ids"] = ["not-in-bundle"]
            return json.dumps(response)

    with pytest.raises(ValueError, match="bundle passage"):
        await analyze_bundle(learning_bundle, context, BadCitationLLM())
