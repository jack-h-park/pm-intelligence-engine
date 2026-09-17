import json
from pathlib import Path

import pytest

from app.models.insights import Candidate, EvidenceBundle, Passage
from app.services.insight_analysis import analyze_bundle
from app.services.insight_context import PreparedContext, load_prepared_context, resolve_interest


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


def _write_interest_context(tmp_path: Path, interests: list[dict[str, object]]) -> None:
    (tmp_path / "core").mkdir(exist_ok=True)
    entries = "\n".join(
        "  - id: {id}\n    question: {question}\n".format(**interest)
        + "".join(
            f"    constraints:\n      - {constraint}\n"
            for constraint in interest.get("constraints", [])
        )
        for interest in interests
    )
    (tmp_path / "core" / "signal-interest-context.yaml").write_text(
        f"revision: fixture-v1\ninterests:\n{entries}", encoding="utf-8"
    )


def test_resolve_interest_returns_first_registered_candidate_interest(tmp_path: Path):
    _write_interest_context(
        tmp_path,
        [
            {"id": "first", "question": "First?", "constraints": ["Bound scope."]},
            {"id": "second", "question": "Second?", "constraints": []},
        ],
    )

    resolved = resolve_interest(["unknown", "second", "first"], tmp_path)

    assert resolved is not None
    assert resolved.id == "second"
    assert resolved.question == "Second?"
    assert resolved.constraints == []
    assert resolved.context_revision == "fixture-v1"


def test_resolve_interest_returns_none_for_unknown_id(tmp_path: Path):
    _write_interest_context(tmp_path, [])

    assert resolve_interest(["unknown"], tmp_path) is None


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

    supplied = json.loads(llm.captured_messages[1]["content"])
    assert supplied["source_ids"] == learning_bundle.source_ids
    assert supplied["evidence"][0]["source_id"] == learning_bundle.passages[0].source_id

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
    assert prepared.question_ids == ["learning-loop"]
    assert prepared.context_paths == ["core/00-pm-identity.md"]
    assert len(prepared.context_hashes["core/00-pm-identity.md"]) == 64
    assert prepared.note_connections == []


@pytest.mark.parametrize(
    ("question_ids", "expected_question", "expected_ids"),
    [
        ([], "A learning lead", []),
        (["unknown", "learning-loop", "another"], "What should I test?", ["learning-loop"]),
    ],
)
def test_context_selects_only_a_resolved_interest_or_unlabelled_subject(
    tmp_path, learning_bundle, question_ids, expected_question, expected_ids
):
    (tmp_path / "core").mkdir()
    (tmp_path / "core/signal-interest-context.yaml").write_text(
        "interests:\n  - id: learning-loop\n    question: What should I test?\n"
        "  - id: another\n    question: What else changed?\n"
    )
    candidate = Candidate(
        candidate_id=learning_bundle.candidate_id, origin="user_supplied",
        subject="A learning lead", question_ids=question_ids, policy_revision="fixture-v1",
    )

    prepared = load_prepared_context(candidate, learning_bundle, tmp_path)

    assert prepared.question == expected_question
    assert prepared.question_ids == expected_ids


@pytest.mark.asyncio
async def test_analysis_preserves_engine_selected_questions_not_model_labels(
    learning_bundle, context
):
    selected = context.model_copy(update={"question_ids": ["learning-loop"]})

    class MislabelledLLM(FixtureLLM):
        async def complete(self, messages, **kwargs):
            payload = json.loads(await super().complete(messages, **kwargs))
            payload["question_ids"] = ["unrelated-model-label"]
            return json.dumps(payload)

    result = await analyze_bundle(learning_bundle, selected, MislabelledLLM())
    assert result.question_ids == ["learning-loop"]


def test_historical_prepared_context_without_question_ids_still_loads(context):
    payload = context.model_dump(mode="json")
    payload.pop("question_ids")
    assert PreparedContext.model_validate(payload).question_ids == []
