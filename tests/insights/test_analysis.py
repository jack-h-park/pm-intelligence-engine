import json
from pathlib import Path

import pytest

from app.models.insights import Candidate, EvidenceBundle, Passage
from app.services.insight_analysis import analyze_bundle
from app.services.insight_context import PreparedContext, load_prepared_context


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
async def test_analysis_prompt_requires_the_complete_insight_json_contract(
    learning_bundle, context
):
    class CapturingLLM(FixtureLLM):
        captured_messages = None

        async def complete(self, messages, **kwargs):
            self.captured_messages = messages
            return await super().complete(messages, **kwargs)

    llm = CapturingLLM()
    await analyze_bundle(learning_bundle, context, llm)

    instruction = llm.captured_messages[0]["content"]
    for field in (
        "headline",
        "explanation",
        "actual_change",
        "why_now",
        "personal_relevance",
        "takeaway",
        "claims",
        "uncertainties",
    ):
        assert field in instruction


@pytest.mark.asyncio
async def test_analysis_rejects_claims_without_bundle_passages(learning_bundle, context):
    class BadCitationLLM(FixtureLLM):
        async def complete(self, messages, **kwargs):
            response = json.loads(await super().complete(messages, **kwargs))
            response["claims"][0]["passage_ids"] = ["not-in-bundle"]
            return json.dumps(response)

    with pytest.raises(ValueError, match="bundle passage"):
        await analyze_bundle(learning_bundle, context, BadCitationLLM())


def test_context_loader_hashes_selected_assets_and_keeps_missing_notes_empty(
    tmp_path: Path, learning_bundle
):
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "signal-interest-context.yaml").write_text(
        "revision: fixture-v1\n"
        "interests:\n"
        "  - id: learning-loop\n"
        "    question: What should I test?\n"
        "    constraints:\n"
        "      - Keep claims attributed.\n"
        "selection:\n"
        "  identity_paths:\n"
        "    - core/00-pm-identity.md\n",
        encoding="utf-8",
    )
    (tmp_path / "core" / "00-pm-identity.md").write_text("Identity context.", encoding="utf-8")
    candidate = Candidate(
        candidate_id=learning_bundle.candidate_id,
        origin="user_supplied",
        subject="A learning lead",
        question_ids=["learning-loop"],
        policy_revision="fixture-v1",
    )

    prepared = load_prepared_context(candidate, learning_bundle, tmp_path)

    assert prepared.question == "What should I test?"
    assert prepared.context_paths == ["core/00-pm-identity.md"]
    assert len(prepared.context_hashes["core/00-pm-identity.md"]) == 64
    assert prepared.note_connections == []
