"""Offline interest re-triage: one comparison table, nothing persisted.

Run as `python -m scripts.retriage_interest ...` from the engine repo root; running the
file path directly fails to import `app`.

Re-runs semantic triage over the stored sources behind an interest's current Insights,
once per candidate question, so a narrowed or new question can be compared before it is
configured. Calls `triage_source` directly: no triage record, no budget reservation.
Run on the ops host against a copy of the engine database. Every call goes through the
real S2K bridge, so this spends model calls; it is run only when authorized.

The variants file is YAML: a list of `{name, question, constraints}`. With
`--decision-context`, the interest's configured definition is compared first as
`current` when it still exists.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.services.insight_context import resolve_interest
from app.services.insight_triage import triage_source

REPO_ROOT = Path(__file__).resolve().parents[1]
DISPOSITIONS = ("admit", "quiet_reference", "defer")


@dataclass(frozen=True)
class Variant:
    name: str
    question: str
    constraints: list[str]


def load_variants(path: str) -> list[Variant]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("variants file must be a non-empty list")
    variants: list[Variant] = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("name") or not item.get("question"):
            raise ValueError("each variant needs a name and a question")
        constraints = item.get("constraints") or []
        if not isinstance(constraints, list) or not all(isinstance(c, str) for c in constraints):
            raise ValueError(f"variant {item['name']}: constraints must be a list of strings")
        variants.append(Variant(str(item["name"]), str(item["question"]), constraints))
    names = [variant.name for variant in variants]
    if len(set(names)) != len(names):
        raise ValueError("variant names must be unique")
    return variants


def with_current(
    variants: list[Variant], interest_id: str, decision_context_root: str | None
) -> list[Variant]:
    if decision_context_root is None or any(v.name == "current" for v in variants):
        return variants
    resolved = resolve_interest([interest_id], decision_context_root)
    if resolved is None:
        return variants
    return [Variant("current", resolved.question, resolved.constraints), *variants]


def select_items(store: Any, interest_id: str, limit: int | None) -> list[dict]:
    """The title and source text triage saw for each current Insight of one interest."""
    items: list[dict] = []
    for insight in store.list_current_insights():
        prepared = store.get_prepared_context(insight.prepared_context_id)
        if prepared is None or interest_id not in prepared.question_ids:
            continue
        candidate = store.get_candidate(prepared.candidate_id)
        sources = [s for s in store.list_sources_for_candidate(prepared.candidate_id) if s.content]
        if candidate is None or not sources:
            continue
        items.append({
            "insight_id": insight.insight_id,
            "headline": insight.headline,
            "title": candidate.subject,
            "url": sources[0].url,
            "content": sources[0].content,
        })
    return items[:limit] if limit is not None else items


async def evaluate(items: list[dict], variants: list[Variant], llm: Any) -> list[dict]:
    rows: list[dict] = []
    for item in items:
        row = {k: item[k] for k in ("insight_id", "headline", "title", "url")}
        row["results"] = {}
        for variant in variants:
            started = time.monotonic()
            try:
                decision = await triage_source(
                    question=variant.question, constraints=variant.constraints,
                    title=item["title"], content=item["content"], llm=llm,
                )
                result: dict[str, Any] = decision.model_dump()
            except Exception as exc:  # one failed call must not end the comparison
                result = {"error": type(exc).__name__}
            result["elapsed_seconds"] = time.monotonic() - started
            row["results"][variant.name] = result
        rows.append(row)
    return rows


def _cell(result: dict) -> str:
    if "error" in result:
        return f"error ({result['error']})"
    return f"{result['disposition']} ({result['relevance']}, {result['novelty']})"


def render(rows: list[dict], variants: list[Variant], *, interest_id: str) -> str:
    lines = [
        "# Interest re-triage comparison",
        "",
        f"Interest: `{interest_id}`",
        f"Sources: {len(rows)}",
        "",
        "| Variant | " + " | ".join(DISPOSITIONS) + " | error |",
        "| --- | " + " | ".join("---" for _ in DISPOSITIONS) + " | --- |",
    ]
    for variant in variants:
        counts = Counter(
            row["results"][variant.name].get("disposition", "error") for row in rows
        )
        lines.append(
            f"| {variant.name} | "
            + " | ".join(str(counts[d]) for d in DISPOSITIONS)
            + f" | {counts['error']} |"
        )
    lines.append("")
    for variant in variants:
        lines += [f"## Variant `{variant.name}`", "", f"- Question: {variant.question}"]
        lines += [f"- Constraint: {c}" for c in variant.constraints]
        lines.append("")
    for row in rows:
        lines += [
            f"## {row['title']}",
            "",
            f"- Insight: `{row['insight_id']}` · {row['headline']}",
            f"- Source: {row['url'] or '?'}",
        ]
        for variant in variants:
            result = row["results"][variant.name]
            lines.append(f"- {variant.name}: {_cell(result)}")
            if result.get("reason"):
                lines.append(f"  - Reason: {result['reason']}")
        lines += ["", "Which variant is right? ", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--interest", required=True)
    parser.add_argument("--variants", required=True)
    parser.add_argument("--decision-context")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    out = Path(args.out).resolve()
    if out.is_relative_to(REPO_ROOT.resolve()):
        print("refusing to write the comparison inside this public repository",
              file=sys.stderr)
        return 2
    if not out.parent.is_dir():
        print(f"output directory does not exist: {out.parent}", file=sys.stderr)
        return 2
    try:
        variants = with_current(
            load_variants(args.variants), args.interest, args.decision_context
        )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"variants file unusable: {exc}", file=sys.stderr)
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
            items = select_items(store, args.interest, args.limit)
        finally:
            store.engine.dispose()
    if not items:
        print(f"no current Insights with stored source text for {args.interest}",
              file=sys.stderr)
        return 1
    rows = asyncio.run(evaluate(items, variants, build_s2k_llm_provider()))
    out.write_text(render(rows, variants, interest_id=args.interest), encoding="utf-8")
    print(f"wrote {len(rows)} sources x {len(variants)} variants to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
