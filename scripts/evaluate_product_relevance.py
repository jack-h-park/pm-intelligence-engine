"""Offline product relevance evaluation: one review table, nothing persisted.

Run as `python -m scripts.evaluate_product_relevance ...` from the engine repo root; running the
file path directly fails to import `app`.

Run on the ops host against a copy of the engine database. Every call goes through the real
S2K bridge, so this spends model calls; it is run only when authorized.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

from app.services.context_loader import ContextLoader
from app.services.insight_product_relevance import Rubric, judge_relevance, load_rubric
from app.services.product_relevance_input import build_product_inputs

REPO_ROOT = Path(__file__).resolve().parents[1]
COST_NOTE = "not reported: the bridge route records provider and model only"


async def evaluate(store: Any, decision_context_root: str, rubric: Rubric, llm: Any) -> list[dict]:
    loaded = build_product_inputs(ContextLoader(decision_context_root), rubric.eligible)
    if loaded.missing:
        listed = ", ".join(f"{pid} ({reason})" for pid, reason in loaded.missing)
        raise ValueError(f"refusing to judge a partial product set: {listed}")
    products = loaded.products
    rows: list[dict] = []
    for insight in store.list_current_insights():
        # Same readability rule as the decision suggestion preview.
        prepared = store.get_prepared_context(insight.prepared_context_id)
        if prepared is None or prepared.validation_status != "valid":
            continue
        bundle = store.get_bundle(prepared.bundle_id)
        if bundle is None:
            continue
        passages = {p.passage_id: p.text for p in bundle.passages}
        diagnostics: dict[str, Any] = {}
        started = time.monotonic()
        verdict = await judge_relevance(
            insight=insight, bundle=bundle, products=products, rubric=rubric, llm=llm,
            diagnostics=diagnostics,
        )
        elapsed = time.monotonic() - started
        cited: list[tuple[str, str]] = []
        for claim in insight.claims:
            for passage_id in claim.passage_ids:
                if passage_id in passages and all(passage_id != seen for seen, _ in cited):
                    cited.append((passage_id, passages[passage_id]))
        rows.append({
            "insight_id": insight.insight_id,
            "headline": insight.headline,
            "decision": verdict.decision,
            "reason": verdict.reason,
            "reason_truncated": bool(diagnostics.get("reason_truncated")),
            "reason_original_length": diagnostics.get("reason_original_length"),
            "quotes_truncated": list(diagnostics.get("quotes_truncated", [])),
            "actual_change": insight.actual_change,
            "takeaway": insight.takeaway,
            "cited_passages": cited,
            "elapsed_seconds": elapsed,
            "links": [{
                "product_id": link.product_id,
                "level": link.level,
                "item_kind": link.item_kind,
                "item_section": link.item_section,
                "item_text": link.item_text,
                "evidence": [{
                    "passage_id": e.passage_id,
                    "quote": e.quote,
                    "passage_text": passages.get(e.passage_id, ""),
                } for e in link.evidence],
            } for link in verdict.links],
            "provider": diagnostics.get("provider"),
            "model": diagnostics.get("model"),
            "status": diagnostics.get("status", "failed"),
            "failures": list(diagnostics.get("failures", [])),
            "cost": COST_NOTE,
        })
    return rows


def _counts(counter: Counter) -> str:
    return ", ".join(f"{key}: {value}" for key, value in sorted(counter.items())) or "none"


def _linked_bucket(count: int) -> str:
    return "3+" if count >= 3 else str(count)


def render(rows: list[dict], *, rubric_revision: str) -> str:
    lines = [
        "# Product relevance offline evaluation",
        "",
        f"Rubric revision: `{rubric_revision}`",
        f"Insights: {len(rows)}",
        "Decisions: " + _counts(Counter(row["decision"] for row in rows)),
        "Rows by linked products: " + ", ".join(
            f"{bucket}: {sum(1 for row in rows if _linked_bucket(len(row['links'])) == bucket)}"
            for bucket in ("0", "1", "2", "3+")
        ),
        "Links by level: " + ", ".join(
            f"{level}: {sum(1 for row in rows for link in row['links'] if link['level'] == level)}"
            for level in ("direct", "related")
        ),
        "Call status: " + _counts(Counter(row["status"] for row in rows)),
        "",
    ]
    for row in rows:
        failures = f" ({'; '.join(row['failures'])})" if row["failures"] else ""
        lines += [
            f"## {row['headline']}",
            "",
            f"- Insight: `{row['insight_id']}`",
            f"- Decision: {row['decision']}",
            f"- Reason: {row['reason']}",
        ]
        if row["reason_truncated"]:
            original = row["reason_original_length"]
            lines.append(f"- Note: reason truncated (original {original} chars)")
        for entry in row["quotes_truncated"]:
            lines.append(
                f"- Note: quote {entry['at']} truncated (original {entry['original_length']} chars)"
            )
        lines += [
            f"- Actual change: {row['actual_change']}",
            f"- Takeaway: {row['takeaway']}",
            f"- Provider / model: {row['provider'] or '?'} / {row['model'] or '?'}",
            f"- Call status: {row['status']}{failures}",
            f"- Elapsed: {row['elapsed_seconds']:.2f}s",
            f"- Cost: {row['cost']}",
        ]
        for passage_id, text in row["cited_passages"]:
            lines.append(f"- Cited passage ({passage_id}): {text}")
        for link in row["links"]:
            lines += [
                f"- Link: {link['product_id']} · {link['level']} · {link['item_kind']}"
                f" · {link['item_section']}",
                f"  - Item: {link['item_text']}",
            ]
            for evidence in link["evidence"]:
                lines += [
                    f"  - Quote ({evidence['passage_id']}): {evidence['quote']}",
                    f"  - Passage: {evidence['passage_text']}",
                ]
            lines += [
                "  - Review: right / overreach",
                "  - Quote supports the link? yes / no",
            ]
        lines += [
            "",
            "Products that should be linked but are not:",
            "Is any level wrong? yes / no",
        ]
        if any(link["item_kind"] == "non_goal" for link in row["links"]):
            lines.append("Reads as a feature to build? yes / no")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--decision-context", required=True)
    parser.add_argument("--rubric", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    out = Path(args.out).resolve()
    if out.is_relative_to(REPO_ROOT.resolve()):
        print("refusing to write the review table inside this public repository",
              file=sys.stderr)
        return 2
    if not out.parent.is_dir():
        print(f"output directory does not exist: {out.parent}", file=sys.stderr)
        return 2
    rubric = load_rubric(args.rubric)
    if rubric is None:
        print("rubric could not be read or has no eligible_products", file=sys.stderr)
        return 2

    unavailable = build_product_inputs(ContextLoader(args.decision_context), rubric.eligible)
    if unavailable.missing:
        for product_id, reason in unavailable.missing:
            print(f"product context unavailable: {product_id} ({reason})", file=sys.stderr)
        return 2

    from app.factory import build_s2k_llm_provider
    from app.storage.insight_store import InsightStore

    with tempfile.TemporaryDirectory() as work:
        copy = Path(work) / "copy.db"
        source = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True)
        target = sqlite3.connect(copy)
        try:
            source.backup(target)
        finally:
            source.close()
            target.close()
        store = InsightStore(f"sqlite:///{copy}")
        try:
            rows = asyncio.run(
                evaluate(store, args.decision_context, rubric, build_s2k_llm_provider())
            )
        finally:
            store.engine.dispose()
    out.write_text(render(rows, rubric_revision=rubric.revision), encoding="utf-8")
    print(f"wrote {len(rows)} rows to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
