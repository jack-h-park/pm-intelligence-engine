"""The offline evaluation: a review table, nothing persisted, never inside the repo."""

import json

import pytest

from app.services.insight_product_relevance import load_rubric
from scripts.evaluate_product_relevance import evaluate, main, render


class FixtureLLM:
    async def complete(self, messages, **kwargs):
        return json.dumps({"decision": "linked", "reason": "It changes isolation.", "links": [{
            "product_id": "android-enterprise", "level": "direct", "item_ref": "android-enterprise/overview/1",
            "evidence": [{"passage_id": "passage-connection", "quote": "managed work profile"}],
        }]})


@pytest.fixture()
def rubric(tmp_path):
    path = tmp_path / "rubric.md"
    path.write_text("---\neligible_products:\n  - android-enterprise\n---\n# R\n",
                    encoding="utf-8")
    return load_rubric(str(path))


@pytest.mark.asyncio
async def test_rows_carry_evidence_status_and_cost_and_nothing_is_persisted(
    tmp_path, engine_with_insight, rubric
):
    engine, insight = engine_with_insight(
        tmp_path, content="A managed work profile permits a selected cross-profile interaction."
    )
    rows = await evaluate(
        engine.insight_store, str(tmp_path / "decision-context"), rubric, FixtureLLM()
    )

    assert [row["insight_id"] for row in rows] == [insight.insight_id]
    row = rows[0]
    assert (row["decision"], row["status"]) == ("linked", "succeeded")
    assert row["links"][0]["item_text"] == "Managed Android."
    assert row["links"][0]["evidence"][0]["passage_text"].startswith("A managed work profile")
    assert row["cost"].startswith("not reported")
    assert engine.insight_store.get_insight(insight.insight_id).product_relevance is None

    table = render(rows, rubric_revision=rubric.revision)
    assert row["links"][0]["level"] == "direct"
    assert rubric.revision in table and "linked: 1" in table
    assert "Rows by linked products: 0: 0, 1: 1, 2: 0, 3+: 0" in table
    assert "Links by level: direct: 1, related: 0" in table
    assert "Link: android-enterprise · direct · overview · " in table
    assert "Review: right / overreach" in table
    assert "Quote supports the link? yes / no" in table
    assert "Products that should be linked but are not:" in table
    assert "Is any level wrong? yes / no" in table
    assert "Review: right / overreach / missed" not in table
    assert "Reads as a feature to build?" not in table


def test_out_inside_the_repository_is_refused(tmp_path):
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    code = main([
        "--database", str(tmp_path / "x.db"), "--decision-context", str(tmp_path),
        "--rubric", str(tmp_path / "r.md"), "--out", str(repo / "relevance-review.md"),
    ])
    assert code == 2
    assert not (repo / "relevance-review.md").exists()


def test_out_in_a_missing_directory_is_refused_before_anything_else(tmp_path, capsys):
    code = main([
        "--database", str(tmp_path / "x.db"), "--decision-context", str(tmp_path),
        "--rubric", str(tmp_path / "r.md"), "--out", str(tmp_path / "missing" / "out.md"),
    ])
    assert code == 2
    assert "output directory does not exist" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_every_row_carries_the_change_takeaway_cited_passages_and_timing(
    tmp_path, engine_with_insight, rubric
):
    class NotRelevantLLM:
        async def complete(self, messages, **kwargs):
            return json.dumps({"decision": "not_relevant", "reason": "y" * 250, "links": []})

    engine, insight = engine_with_insight(
        tmp_path, content="A managed work profile permits a selected cross-profile interaction."
    )
    rows = await evaluate(
        engine.insight_store, str(tmp_path / "decision-context"), rubric, NotRelevantLLM()
    )
    row = rows[0]
    assert row["decision"] == "not_relevant" and row["links"] == []
    assert row["actual_change"] == insight.actual_change
    assert row["takeaway"] == insight.takeaway
    assert row["cited_passages"]
    assert all(pid and text for pid, text in row["cited_passages"])
    assert len({pid for pid, _ in row["cited_passages"]}) == len(row["cited_passages"])
    assert row["elapsed_seconds"] >= 0
    assert row["reason_truncated"] is True and row["reason_original_length"] == 250

    table = render(rows, rubric_revision=rubric.revision)
    assert insight.actual_change in table and insight.takeaway in table
    for pid, text in row["cited_passages"]:
        assert pid in table and text in table
    assert "reason truncated (original 250 chars)" in table
    assert "Elapsed" in table
    assert "Products that should be linked but are not:" in table
    assert "Is any level wrong? yes / no" in table
    assert "Rows by linked products: 0: 1, 1: 0, 2: 0, 3+: 0" in table
    assert "Links by level: direct: 0, related: 0" in table
    assert "Quote supports the link?" not in table


@pytest.mark.asyncio
async def test_evaluate_refuses_a_partial_product_set(tmp_path, engine_with_insight, rubric):
    calls = []

    class CountingLLM:
        async def complete(self, messages, **kwargs):
            calls.append(1)
            return "{}"

    engine, _ = engine_with_insight(tmp_path, content="A managed work profile.")
    partial = load_rubric(_write_rubric(tmp_path, ["android-enterprise", "absent-product"]))
    with pytest.raises(ValueError, match="absent-product"):
        await evaluate(
            engine.insight_store, str(tmp_path / "decision-context"), partial, CountingLLM()
        )
    assert calls == []


def _write_rubric(tmp_path, eligible):
    path = tmp_path / "partial-rubric.md"
    body = "".join(f"  - {item}\n" for item in eligible)
    path.write_text(f"---\neligible_products:\n{body}---\n# R\n", encoding="utf-8")
    return str(path)


def test_main_refuses_a_partial_set_before_any_model_call_or_database_open(
    tmp_path, capsys, monkeypatch
):
    import app.factory as factory

    def boom(*args, **kwargs):
        raise AssertionError("must not be reached")

    monkeypatch.setattr(factory, "build_s2k_llm_provider", boom)
    root = tmp_path / "dc"
    (root / "products" / "android-enterprise").mkdir(parents=True)
    (root / "products" / "android-enterprise" / "context.md").write_text(
        "# A\n\n## Product Overview\nA.\n", encoding="utf-8"
    )
    rubric_path = _write_rubric(tmp_path, ["android-enterprise", "absent-product"])
    code = main([
        "--database", str(tmp_path / "does-not-exist.db"), "--decision-context", str(root),
        "--rubric", rubric_path, "--out", str(tmp_path / "out.md"),
    ])
    err = capsys.readouterr().err
    assert code == 2
    assert "absent-product" in err and "missing" in err
    assert not (tmp_path / "out.md").exists()
