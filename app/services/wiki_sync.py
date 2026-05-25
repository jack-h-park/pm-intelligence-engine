"""Wiki sync service — utility adapter for WIKI_ROOT file operations.

Ownership (per EXPORT_AND_SYNC_CONTRACT.md):
  - decision-system export: pm-platform (via run_exporter.py)
  - wiki sync: Hermes operations plane (NOT pm-platform completion paths)

This module is a utility/legacy adapter. pm-platform completion paths do NOT call
sync_executive_summary() directly. Hermes consumes completed run events and calls
these helpers (or its own equivalent) when writing to the wiki.

Canonical wiki target paths:
  prd   → WIKI_ROOT/raw/from-decision-system/prds/<date>-<slug>.md
  poc   → WIKI_ROOT/raw/from-decision-system/poc-upgrades/<date>-<slug>.md
  kill  → WIKI_ROOT/raw/from-decision-system/kills/<date>-<slug>.md

Auto-triage archive (wiki only, no decision-system export):
  WIKI_ROOT/raw/from-decision-system/kills/auto-triaged/<date>-<slug>.md

Modes that are NOT wiki sync targets: brief, opportunity, evaluate.
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
    """Write an auto-triaged signal to
    WIKI_ROOT/raw/from-decision-system/kills/auto-triaged/.

    The file is written silently. If the wiki root does not exist (e.g. running
    in a CI environment without the external repo mounted), the error is swallowed
    and logged — a missing archive must never fail the run itself.

    Returns the path written, or raises on unexpected errors after logging.
    """
    try:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        slug = _slugify(signal_title)
        filename = f"{date_str}-{slug}.md"

        target_dir = (
            Path(wiki_root) / "raw" / "from-decision-system" / "kills" / "auto-triaged"
        )
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
# Executive summary sync (Hermes-owned; this is a utility helper)
# ---------------------------------------------------------------------------


def sync_executive_summary(
    run_id: str,
    product_id: str,
    routing: str,
    markdown: str,
    signal_title: str,
    wiki_root: str,
    run_folder: str = "",
) -> Path:
    """Write a completed run's Executive Summary to the canonical wiki path.

    routing == 'prd'  → raw/from-decision-system/prds/<date>-<slug>.md
    routing == 'poc'  → raw/from-decision-system/poc-upgrades/<date>-<slug>.md
    routing == 'kill' → raw/from-decision-system/kills/<date>-<slug>.md

    NOTE: pm-platform completion paths do not call this directly.
    This is provided for Hermes (or manual use) as a utility helper.
    """
    routing_to_dir = {
        "prd": "prds",
        "poc": "poc-upgrades",
        "kill": "kills",
    }
    subdir = routing_to_dir.get(routing)
    if subdir is None:
        raise ValueError(
            f"Unknown routing '{routing}' for wiki sync. "
            f"Expected one of: {', '.join(routing_to_dir)}"
        )

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    slug = _slugify(signal_title)
    filename = f"{date_str}-{slug}.md"

    target_dir = Path(wiki_root) / "raw" / "from-decision-system" / subdir
    target_dir.mkdir(parents=True, exist_ok=True)

    target_path = target_dir / filename
    target_path.write_text(
        _add_frontmatter(markdown, run_id, product_id, routing, date_str, run_folder),
        encoding="utf-8",
    )
    return target_path


def sync_executive_summary_safe(
    run_id: str,
    product_id: str,
    routing: str,
    markdown: str,
    signal_title: str,
    wiki_root: str,
    run_folder: str = "",
) -> None:
    """Non-fatal wrapper around sync_executive_summary().

    Swallows OSError (wiki root not mounted / not writable) and logs the skip
    via emit_event. All other exceptions propagate so real bugs surface.
    """
    from app.logging import emit_event

    try:
        path = sync_executive_summary(
            run_id=run_id,
            product_id=product_id,
            routing=routing,
            markdown=markdown,
            signal_title=signal_title,
            wiki_root=wiki_root,
            run_folder=run_folder,
        )
        emit_event("wiki_sync", "summary_synced", run_id, {
            "path": str(path),
            "routing": routing,
        })
    except OSError:
        emit_event("wiki_sync", "sync_skipped", run_id, {
            "reason": "wiki_root not writable",
            "wiki_root": wiki_root,
            "routing": routing,
        })


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
    run_folder: str = "",
) -> str:
    """Prepend wiki frontmatter conforming to jackhpark-product-management-wiki/CLAUDE.md.

    Required fields (from wiki CLAUDE.md schema):
      source: jackhpark-pm-decision-system
      run: <run-folder-name>   ← the exports directory name, e.g. 2026-05-25-android16-nfc
      type: kill | prd | poc-upgrade
      date: YYYY-MM-DD
      review_needed: false

    The `routing` value maps to `type` as: prd→prd, poc→poc-upgrade, kill→kill.
    `run_folder` should be the <YYYY-MM-DD>-<slug> directory name written by run_exporter.
    If not provided, falls back to run_id.
    """
    routing_to_type = {"prd": "prd", "poc": "poc-upgrade", "kill": "kill"}
    wiki_type = routing_to_type.get(routing, routing)
    run_name = run_folder if run_folder else run_id

    frontmatter = (
        f"---\n"
        f"source: jackhpark-pm-decision-system\n"
        f"run: {run_name}\n"
        f"type: {wiki_type}\n"
        f"date: {date_str}\n"
        f"review_needed: false\n"
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
