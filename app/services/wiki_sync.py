"""Wiki sync service — writes platform outputs to WIKI_ROOT.

Two responsibilities:
1. archive_auto_triaged() — stores low-relevance signals in raw/kills/auto-triaged/
   so the PM can spot-check auto-dropped signals without being interrupted.
2. sync_executive_summary() — writes Stage 7 Executive Summary output to the
   appropriate raw/ subdirectory after a full run completes (planned for Phase 4.3).
"""

import re
from datetime import datetime, timezone
from pathlib import Path

from app.models.stages import S2OutputData


# ---------------------------------------------------------------------------
# Auto-triage archive
# ---------------------------------------------------------------------------


def archive_auto_triaged(
    run_id: str,
    product_id: str,
    signal_title: str,
    s2_output: S2OutputData,
    wiki_root: str,
) -> Path:
    """Write an auto-triaged signal to WIKI_ROOT/raw/kills/auto-triaged/.

    The file is written silently. If the wiki root does not exist (e.g. running
    in a CI environment without the external repo mounted), the error is swallowed
    and logged — a missing archive must never fail the run itself.

    Returns the path written, or raises on unexpected errors after logging.
    """
    try:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        slug = _slugify(signal_title)
        filename = f"{date_str}-{slug}.md"

        target_dir = Path(wiki_root) / "raw" / "kills" / "auto-triaged"
        target_dir.mkdir(parents=True, exist_ok=True)

        content = _render_auto_triaged(
            run_id=run_id,
            product_id=product_id,
            signal_title=signal_title,
            s2_output=s2_output,
            date_str=date_str,
        )

        target_path = target_dir / filename
        target_path.write_text(content, encoding="utf-8")
        return target_path

    except OSError:
        # Wiki root not mounted or directory not writable — non-fatal.
        from app.logging import emit_event
        emit_event("wiki_sync", "archive_skipped", run_id, {
            "reason": "wiki_root not writable",
            "wiki_root": wiki_root,
        })
        raise


# ---------------------------------------------------------------------------
# Executive summary sync (Phase 4.3 — placeholder)
# ---------------------------------------------------------------------------


def sync_executive_summary(
    run_id: str,
    product_id: str,
    routing: str,
    markdown: str,
    signal_title: str,
    wiki_root: str,
) -> Path:
    """Write a completed run's Executive Summary to the appropriate wiki subdirectory.

    routing == 'prd'  → raw/prds/<date>-<slug>.md
    routing == 'poc'  → raw/poc-upgrades/<date>-<slug>.md
    routing == 'kill' → raw/kills/<date>-<slug>.md
    """
    routing_to_dir = {
        "prd": "prds",
        "poc": "poc-upgrades",
        "kill": "kills",
    }
    subdir = routing_to_dir.get(routing, "kills")

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    slug = _slugify(signal_title)
    filename = f"{date_str}-{slug}.md"

    target_dir = Path(wiki_root) / "raw" / subdir
    target_dir.mkdir(parents=True, exist_ok=True)

    target_path = target_dir / filename
    target_path.write_text(_add_frontmatter(markdown, run_id, product_id, routing, date_str), encoding="utf-8")
    return target_path


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _render_auto_triaged(
    run_id: str,
    product_id: str,
    signal_title: str,
    s2_output: S2OutputData,
    date_str: str,
) -> str:
    pillars = ", ".join(s2_output.pillar_references) if s2_output.pillar_references else "N/A"
    return f"""---
run_id: {run_id}
product_id: {product_id}
date: {date_str}
relevance_score: {s2_output.relevance_score}/5
triage: auto
---

# Auto-Triaged Signal

**Title:** {signal_title}
**Product:** {product_id}
**Date:** {date_str}
**Relevance score:** {s2_output.relevance_score}/5 (auto-triaged — below threshold)

---

## S2 Insight

**What changed:**
{s2_output.what_changed}

**Reframing:**
{s2_output.reframing}

**Why this was auto-triaged:**
{s2_output.suggestion_reasoning}

**Strategy pillars referenced:** {pillars}

---

*This signal was automatically archived without PM review because its relevance
score ({s2_output.relevance_score}/5) fell below the configured threshold.
To process it, submit a new run with `mode: "evaluate"` set explicitly.*
"""


def _add_frontmatter(
    markdown: str,
    run_id: str,
    product_id: str,
    routing: str,
    date_str: str,
) -> str:
    frontmatter = (
        f"---\n"
        f"run_id: {run_id}\n"
        f"product_id: {product_id}\n"
        f"routing: {routing}\n"
        f"date: {date_str}\n"
        f"---\n\n"
    )
    return frontmatter + markdown


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"\s+", "-", text.strip())
    text = re.sub(r"-+", "-", text)
    return text[:60].rstrip("-")
