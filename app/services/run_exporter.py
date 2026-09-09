"""Export a completed run to the canonical pm-engine archive.

Canonical output directory structure:
  archive/runs/<product_id>/<YYYY-MM-DD>-<slug>/
  ├── s1-signal.md
  ├── s2-insight.md
  ├── s3-opportunity.md
  ├── s4-evaluation.md
  ├── s5-prioritization.md
  ├── s6-poc-plan.md  (routing == poc)
  ├── s6-prd.md       (routing == prd)
  └── s7-report.md
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.models.stages import (
    S1OutputData,
    S2OutputData,
    S3OutputData,
    S4OutputData,
    S5OutputData,
    S6AOutputData,
    S6BOutputData,
    S7OutputData,
)
from app.storage.protocol import PMWorkflowStore


def export_run(
    run_id: str,
    store: PMWorkflowStore,
    decision_system_root: str,
) -> Path:
    """Export all stage outputs for a run to the canonical archive path.

    Returns the canonical pm-engine archive directory path that was written.
    """
    run = store.get_run(run_id)
    if run is None:
        raise ValueError(f"Run {run_id} not found")

    product_id = run["product_id"]
    # Folder date = run terminal date (completed_at), else created_at, else now().
    # Keeps backfilled exports on the run real date, not the export date.
    _run_date = run.get("completed_at") or run.get("created_at")
    date_str = _run_date[:10] if _run_date else datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Load all stage outputs
    s1 = _load_stage(store, run_id, "s1", S1OutputData)
    s2 = _load_stage(store, run_id, "s2", S2OutputData)
    s3 = _load_stage(store, run_id, "s3", S3OutputData)
    s4 = _load_stage(store, run_id, "s4", S4OutputData)
    s5 = _load_stage(store, run_id, "s5", S5OutputData)
    s6a = _load_stage(store, run_id, "s6a", S6AOutputData)
    s6b = _load_stage(store, run_id, "s6b", S6BOutputData)
    s7 = _load_stage(store, run_id, "s7", S7OutputData)

    slug = _slugify(s1.title if s1 else run_id)
    run_folder_name = f"{date_str}-{slug}"
    canonical_run_dir = _canonical_archive_root() / product_id / run_folder_name

    _write_run_artifacts(
        canonical_run_dir,
        date_str=date_str,
        s1=s1,
        s2=s2,
        s3=s3,
        s4=s4,
        s5=s5,
        s6a=s6a,
        s6b=s6b,
        s7=s7,
    )

    return canonical_run_dir


# ---------------------------------------------------------------------------
# Stage renderers
# ---------------------------------------------------------------------------


def _sections_of(memo_md: str) -> str:
    """The body of a stage's canonical markdown, from its first `## ` heading.

    The archive file and the DB artifact are two presentations of one stage
    output. Rendering them from separate templates meant the same facts were
    phrased three different ways, and the Observatory could not tell they were
    the same document — its duplicate check compares from the first `## `
    onward, so differing section headings kept both copies on screen as if the
    reader had a choice to make. The stage module now owns the body; the archive
    file adds only its own heading and metadata block above it, which sits above
    that comparison point and so stays free to differ.
    """
    i = memo_md.find("\n## ")
    return memo_md[i + 1 :] if i >= 0 else memo_md


def _archive_doc(heading: str, meta_lines: list[str], memo_md: str) -> str:
    meta = "\n".join(meta_lines)
    return f"""# {heading}

{meta}

---

{_sections_of(memo_md)}"""


def _render_s1(s1: S1OutputData, date_str: str) -> str:
    return f"""# Stage 1: Signal Ingestion

**Signal ID:** {s1.signal_id}
**Date logged:** {date_str}

---

## Signal Entry

| Field | Value |
|-------|-------|
| Source | {s1.source} |
| Event date | {s1.event_date or date_str} |
| Category | {s1.category} |

## Description

{s1.summary}
"""


def _render_s2(s2: S2OutputData, s1: S1OutputData, date_str: str) -> str:
    from app.stages.s2_insight import build_insight_memo

    return _archive_doc(
        "Stage 2: Insight Extraction",
        [
            f"**Signal ref:** {s1.signal_id} ({s1.title})",
            f"**Date:** {date_str}",
            f"**Relevance score:** {s2.relevance_score}/5",
        ],
        build_insight_memo(s1.title, s1.category, s2),
    )


def _render_s3(s3: S3OutputData, date_str: str) -> str:
    from app.stages.s3_opportunity import build_opportunity_memo

    return _archive_doc("Stage 3: Opportunity Creation", [f"**Date:** {date_str}"], build_opportunity_memo(s3))


def _render_s4(s4: S4OutputData, date_str: str) -> str:
    from app.stages.s4_evaluation import build_evaluation_brief

    return _archive_doc(
        "Stage 4: Persona Evaluation",
        [f"**Date:** {date_str}"],
        build_evaluation_brief(s4.personas, s4.rubric, getattr(s4, "disagreement_matrix", None)),
    )


def _render_s5(s5: S5OutputData, date_str: str) -> str:
    from app.stages.s5_prioritization import build_decision_memo

    return _archive_doc("Stage 5: Prioritization", [f"**Date:** {date_str}"], build_decision_memo(s5))


def _render_s6a(s6a: S6AOutputData, date_str: str) -> str:
    from app.stages.s6a_poc_plan import build_poc_plan

    return _archive_doc("Stage 6A: PoC Plan", [f"**Date:** {date_str}"], build_poc_plan(s6a))


def _render_s6b(s6b: S6BOutputData, date_str: str) -> str:
    from app.stages.s6b_prd import build_prd

    return _archive_doc("Stage 6B: PRD", [f"**Date:** {date_str}"], build_prd(s6b))


def _load_stage(
    store: PMWorkflowStore,
    run_id: str,
    stage: str,
    model_class,  # type: ignore[no-untyped-def]
) -> Optional[object]:
    raw = store.get_stage_output(run_id, stage)
    if raw is None:
        return None
    data = json.loads(raw["output_json"])
    return model_class(**data["output"])


def _canonical_archive_root() -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "archive" / "runs"


def _write_run_artifacts(
    run_dir: Path,
    *,
    date_str: str,
    s1: S1OutputData | None,
    s2: S2OutputData | None,
    s3: S3OutputData | None,
    s4: S4OutputData | None,
    s5: S5OutputData | None,
    s6a: S6AOutputData | None,
    s6b: S6BOutputData | None,
    s7: S7OutputData | None,
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)

    if s1:
        (run_dir / "s1-signal.md").write_text(_render_s1(s1, date_str), encoding="utf-8")
    if s2 and s1:
        (run_dir / "s2-insight.md").write_text(_render_s2(s2, s1, date_str), encoding="utf-8")
    if s3:
        (run_dir / "s3-opportunity.md").write_text(_render_s3(s3, date_str), encoding="utf-8")
    if s4:
        (run_dir / "s4-evaluation.md").write_text(_render_s4(s4, date_str), encoding="utf-8")
    if s5:
        (run_dir / "s5-prioritization.md").write_text(_render_s5(s5, date_str), encoding="utf-8")
    if s6a:
        (run_dir / "s6-poc-plan.md").write_text(_render_s6a(s6a, date_str), encoding="utf-8")
    if s6b:
        (run_dir / "s6-prd.md").write_text(_render_s6b(s6b, date_str), encoding="utf-8")
    if s7:
        (run_dir / "s7-report.md").write_text(s7.markdown, encoding="utf-8")


def _slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s]+", "-", text.strip())
    text = re.sub(r"-+", "-", text)
    return text[:60].rstrip("-")
