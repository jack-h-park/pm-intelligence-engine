"""Portfolio Synthesis (US-49, Variant 2) — post-hoc cross-product memo.

When every run in a fan-out batch has settled, one LLM call reads across all of
them and produces a portfolio-level memo (priority ranking, shared root cause,
sequencing, conflicts, synergies). Visibility only — it does not change any
product's routing.

Triggered from run_finalizer (the single terminal exit point). Persisted via the
store's insert-or-skip guard, so near-simultaneous batch completion synthesizes
exactly once. pm-engine writes no files — it emits ``portfolio_synthesized`` and
The operations plane owns any wiki sync (EXPORT_AND_SYNC_CONTRACT.md).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from app.llm.json_call import complete_json
from app.logging import emit_event
from app.models.stages import PortfolioSynthesisData

if TYPE_CHECKING:
    from app.factory import PMEngine


_JSON_SCHEMA = """{
  "priority_ranking": [{"product_id": "<id>", "rank": <int, 1=highest>, "rationale": "<one line>"}],
  "shared_root_cause": "<single underlying change across products, or empty>",
  "sequencing": "<order / dependencies between the efforts, or empty>",
  "resource_conflicts": "<contention for team/capacity/window, or empty>",
  "synergies": "<work that could serve several products, or empty>",
  "recommendation": "<one paragraph: how to hold these as a portfolio response>"
}"""


def _run_summary(run: dict, engine: PMEngine) -> str:
    """A compact per-product block for the synthesis prompt."""
    routing = run.get("routing") or "—"
    composite = run.get("composite_score")
    composite_s = f"{composite}" if composite is not None else "—"

    what_changed = ""
    relevance = "—"
    s2 = engine.store.get_stage_output(run["run_id"], "s2")
    if s2 is not None:
        out = json.loads(s2["output_json"]).get("output", {})
        what_changed = out.get("what_changed", "")
        relevance = out.get("relevance_score", "—")

    return (
        f"### {run['product_id']}\n"
        f"Outcome: {run.get('outcome') or '—'} · routing: {routing} · "
        f"composite: {composite_s} · relevance: {relevance}\n"
        f"What changed for this product: {what_changed}"
    )


def _build_memo(
    signal_title: str, runs: list[dict], data: PortfolioSynthesisData
) -> str:
    ranking_lines = "\n".join(
        f"{item.rank}. **{item.product_id}** — {item.rationale}"
        for item in sorted(data.priority_ranking, key=lambda i: i.rank)
    ) or "—"
    products = ", ".join(r["product_id"] for r in runs)
    return f"""# Portfolio Synthesis — {signal_title}

**Products affected ({len(runs)}):** {products}

## Portfolio Priority
{ranking_lines}

## Shared Root Cause
{data.shared_root_cause or "—"}

## Sequencing & Dependencies
{data.sequencing or "—"}

## Resource Conflicts
{data.resource_conflicts or "—"}

## Synergies
{data.synergies or "—"}

## Recommendation
{data.recommendation or "—"}

---
*Visibility only — each product keeps the routing its own run reached.*
"""


def batch_ready_for_synthesis(batch_id: str, engine: PMEngine) -> bool:
    """True iff the batch is closed for membership, has >1 run, all runs are

    settled, and no synthesis exists yet.
    """
    batch = engine.store.get_batch(batch_id)
    if batch is None or not batch["membership_closed"]:
        return False
    if engine.store.get_portfolio_synthesis(batch_id) is not None:
        return False
    runs = engine.store.list_runs(batch_id=batch_id, limit=1000)
    if len(runs) <= 1:
        return False
    # A run is settled once it is terminal (lifecycle=done; US-55).
    return all(r.get("lifecycle") == "done" for r in runs)


async def synthesize_batch(batch_id: str, engine: PMEngine) -> bool:
    """Produce and persist the portfolio memo for a settled batch.

    Returns True if a synthesis was written, False if skipped (already present —
    the insert-or-skip idempotency guard) or the batch is a single run.
    """
    runs = engine.store.list_runs(batch_id=batch_id, limit=1000)
    if len(runs) <= 1:
        return False

    batch = engine.store.get_batch(batch_id)
    signal = engine.store.get_signal(batch["signal_id"]) if batch else None
    signal_title = signal["title"] if signal else batch_id
    signal_id = batch["signal_id"] if batch else ""

    from config import settings
    from app.services.template_service import TemplateService

    framework = TemplateService(settings.DECISION_SYSTEM_ROOT).load_portfolio_prompt(
        "synthesis"
    )
    pm_identity = engine.context_loader.load_pm_identity()

    blocks = "\n\n".join(_run_summary(r, engine) for r in runs)
    system_message = (
        "You are a Product Manager. Follow the PM identity and operating philosophy "
        f"below.\n\n{pm_identity}"
    )
    user_message = f"""## Portfolio Synthesis Framework
{framework}

---

## Signal
{signal_title}

---

## Per-Product Results
{blocks}

---

## Your Task
Produce the portfolio reading across the products above.
Respond with a single JSON object matching this schema exactly — no markdown, no commentary:

{_JSON_SCHEMA}"""

    raw = await complete_json(
        engine.llm,
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ],
        stage="portfolio_synthesis",
        run_id=batch_id,
        max_tokens=1536,
        temperature=0,
    )
    data = PortfolioSynthesisData(**raw)
    content_md = _build_memo(signal_title, runs, data)

    inserted = engine.store.save_portfolio_synthesis(
        batch_id=batch_id,
        signal_id=signal_id,
        content_md=content_md,
        content_json=data.model_dump_json(),
        run_ids_json=json.dumps([r["run_id"] for r in runs]),
    )
    if inserted:
        emit_event(
            "portfolio", "synthesized", signal_id,
            {"batch_id": batch_id, "products": [r["product_id"] for r in runs]},
        )
    return inserted
