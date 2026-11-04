"""Offline re-triage: one interest's stored sources, compared per question, unpersisted."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.models.insights import (
    Candidate,
    EvidenceBundle,
    InsightClaim,
    InsightRevision,
    Passage,
    PreparedContext,
    SourceRecord,
)
from app.storage.insight_store import InsightStore
from scripts.retriage_interest import (
    Variant,
    evaluate,
    load_variants,
    main,
    render,
    select_items,
    with_current,
)


class FixtureLLM:
    """Admits only under the narrowed question, so the two variants disagree."""

    async def complete(self, messages, **kwargs):
        narrowed = any("own product decisions" in m["content"] for m in messages)
        return json.dumps({
            "disposition": "admit" if narrowed else "quiet_reference",
            "relevance": "relevant" if narrowed else "adjacent",
            "novelty": "meaningful_delta",
            "reason": "Fixture reason.",
        })


class FailingLLM:
    async def complete(self, messages, **kwargs):
        raise TimeoutError("bridge timed out")


def _save(store: InsightStore, key: str, question_ids: list[str], content: str) -> None:
    candidate = store.save_candidate(Candidate(
        candidate_id=f"candidate-{key}", origin="discovered", subject=f"Subject {key}",
        question_ids=question_ids, policy_revision="fixture-v1",
    ).model_dump())
    source = store.save_source(SourceRecord(
        source_id=f"source-{key}", candidate_id=candidate.candidate_id, origin="discovered",
        content_hash=hashlib.sha256(content.encode()).hexdigest(), acquisition_status="ok",
        retrieved_at=datetime(2026, 10, 1, tzinfo=UTC), content=content,
        url=f"https://example.test/{key}",
    ).model_dump(mode="json"))
    bundle = store.save_bundle(EvidenceBundle(
        bundle_id=f"bundle-{key}", candidate_id=candidate.candidate_id,
        source_ids=[source.source_id],
        passages=[Passage(passage_id=f"passage-{key}", source_id=source.source_id,
                          locator="fixture", text=content, role="seed")],
        freshness_status="current", context_revision="fixture-v1",
    ).model_dump(mode="json"))
    prepared = store.save_prepared_context(PreparedContext(
        prepared_context_id=f"prepared-{key}", candidate_id=candidate.candidate_id,
        bundle_id=bundle.bundle_id, question="What changed?", question_ids=question_ids,
        validation_status="valid", context_revision="fixture-v1",
    ).model_dump(mode="json"))
    store.save_insight(InsightRevision(
        insight_id=f"insight-{key}", prepared_context_id=prepared.prepared_context_id,
        headline=f"Headline {key}", explanation="A bounded fixture finding.",
        actual_change="Something changed.", why_now="The fixture is current.",
        personal_relevance="Fixture relevance.", takeaway="Review it.",
        claims=[InsightClaim(text="Cited.", passage_ids=[f"passage-{key}"])],
        context_revision="fixture-v1",
    ).model_dump(mode="json"))


@pytest.fixture()
def store(tmp_path):
    store = InsightStore(f"sqlite:///{tmp_path}/insights.db")
    store.initialize_schema()
    _save(store, "ems", ["enterprise-mobile-security"], "A managed-device control changed.")
    _save(store, "other", ["personal-ai-agent-market"], "An assistant launched.")
    yield store
    store.engine.dispose()


def _decision_context(root: Path) -> str:
    (root / "core").mkdir(parents=True)
    (root / "core" / "signal-interest-context.yaml").write_text(
        "revision: fixture\ninterests:\n"
        "  - id: enterprise-mobile-security\n"
        "    question: What changed in mobile security?\n"
        "    constraints:\n      - Keep scope explicit.\n",
        encoding="utf-8",
    )
    return str(root)


NARROWED = Variant("narrowed", "What matters for my own product decisions?", ["Name it."])


@pytest.mark.asyncio
async def test_only_the_named_interest_is_retriaged_under_every_variant(tmp_path, store):
    items = select_items(store, "enterprise-mobile-security", None)
    assert [(i["insight_id"], i["title"], i["url"]) for i in items] == [
        ("insight-ems", "Subject ems", "https://example.test/ems"),
    ]
    variants = with_current([NARROWED], "enterprise-mobile-security",
                            _decision_context(tmp_path / "dc"))
    assert [v.name for v in variants] == ["current", "narrowed"]
    assert variants[0].constraints == ["Keep scope explicit."]

    rows = await evaluate(items, variants, FixtureLLM())
    results = rows[0]["results"]
    assert (results["current"]["disposition"], results["narrowed"]["disposition"]) == (
        "quiet_reference", "admit",
    )

    table = render(rows, variants, interest_id="enterprise-mobile-security")
    assert "| current | 0 | 1 | 0 | 0 |" in table
    assert "| narrowed | 1 | 0 | 0 | 0 |" in table
    assert "- narrowed: admit (relevant, meaningful_delta)" in table
    assert "- Question: What matters for my own product decisions?" in table


@pytest.mark.asyncio
async def test_a_failed_call_is_recorded_and_the_comparison_continues(store):
    items = select_items(store, "enterprise-mobile-security", None)
    rows = await evaluate(items, [NARROWED], FailingLLM())
    assert rows[0]["results"]["narrowed"]["error"] == "TimeoutError"
    table = render(rows, [NARROWED], interest_id="enterprise-mobile-security")
    assert "| narrowed | 0 | 0 | 0 | 1 |" in table


def test_a_retired_interest_has_no_current_variant(tmp_path):
    variants = with_current([NARROWED], "learning-loop", _decision_context(tmp_path / "dc"))
    assert variants == [NARROWED]


@pytest.mark.parametrize("content, message", [
    ("[]", "non-empty list"),
    ("- name: a\n", "name and a question"),
    ("- {name: a, question: q, constraints: x}\n", "list of strings"),
    ("- {name: a, question: q}\n- {name: a, question: r}\n", "unique"),
])
def test_unusable_variants_files_are_refused(tmp_path, content, message):
    path = tmp_path / "variants.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_variants(str(path))


def test_out_inside_the_repository_is_refused(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    code = main([
        "--database", str(tmp_path / "x.db"), "--interest", "x",
        "--variants", str(tmp_path / "v.yaml"), "--out", str(repo / "retriage.md"),
    ])
    assert code == 2
    assert not (repo / "retriage.md").exists()
