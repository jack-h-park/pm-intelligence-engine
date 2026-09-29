"""The offline evaluation: a review table, nothing persisted, never inside the repo."""

import json

import pytest

from app.services.insight_product_relevance import load_rubric
from scripts.evaluate_product_relevance import evaluate, main, render


class FixtureLLM:
    async def complete(self, messages, **kwargs):
        return json.dumps({"decision": "relevant", "reason": "It changes isolation.", "links": [{
            "product_id": "android-enterprise", "item_ref": "android-enterprise/overview/1",
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
    assert (row["decision"], row["status"]) == ("relevant", "succeeded")
    assert row["links"][0]["item_text"] == "Managed Android."
    assert row["links"][0]["evidence"][0]["passage_text"].startswith("A managed work profile")
    assert row["cost"].startswith("not reported")
    assert engine.insight_store.get_insight(insight.insight_id).product_relevance is None

    table = render(rows, rubric_revision=rubric.revision)
    assert rubric.revision in table and "relevant: 1" in table
    assert "Link supported by the quoted passage? yes / no" in table
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
