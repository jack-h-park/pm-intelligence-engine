"""Export a completed run to the canonical pm-engine archive.

Canonical output directory structure:
  archive/runs/<product_id>/<YYYY-MM-DD>-<slug>/
  ├── s1-signal.md
  ├── s2-insight.md
  ├── s3-opportunity.md
  ├── s4-evaluation.md
  ├── s4-evaluation-rubric-score.md
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
    pillars = ", ".join(s2.pillar_references) if s2.pillar_references else "N/A"
    return f"""# Stage 2: Insight Extraction

**Signal ref:** {s1.signal_id} ({s1.title})
**Date:** {date_str}
**Relevance score:** {s2.relevance_score}/5

---

## What Changed?

{s2.what_changed}

## Reframing Check

{s2.reframing}

## Why Does This Matter?

{s2.relevance_explanation}

**Strategy pillars referenced:** {pillars}
"""


def _render_s3(s3: S3OutputData, date_str: str) -> str:
    return f"""# Stage 3: Opportunity Creation

**Date:** {date_str}

---

## Problem Statement

{s3.problem_statement}

## Target User

{s3.target_user}

## Core Hypothesis

{s3.hypothesis}

## Assumed Value

**For the customer:**
{s3.assumed_value_user}

**For Samsung:**
{s3.assumed_value_business}
"""


def _render_s4(s4: S4OutputData, date_str: str) -> str:
    sections = []
    for p in s4.personas:
        dim_label = p.dimension
        sections.append(f"""## {p.persona.capitalize()} — Dimension: {dim_label}

{p.key_argument}

**Score: {p.score}/5**
**Open question:** {p.open_question}
""")

    body = "\n---\n\n".join(sections)
    return f"""# Stage 4: Persona Evaluation

**Date:** {date_str}

---

{body}"""


def _render_s4_rubric(s4: S4OutputData, date_str: str) -> str:
    r = s4.rubric
    issues_md = ""
    if r.issues:
        issues_lines = "\n".join(f"- {i}" for i in r.issues)
        issues_md = f"\n### Issues\n\n{issues_lines}\n"

    status = "Pass" if r.passed else "Fail"
    return f"""# Stage 4: Persona Evaluation — Rubric Score

**Date:** {date_str}

---

| Dimension | Score |
|-----------|-------|
| Score Grounding | {r.score_grounding}/3 |
| Skeptic Quality | {r.skeptic_quality}/3 |
| Open Question Quality | {r.open_question_quality}/3 |
| Persona Independence | {r.persona_independence}/3 |

**Total: {r.total_score}/12 — {status}**
{issues_md}"""


def _render_s5(s5: S5OutputData, date_str: str) -> str:
    # Scores table
    score_rows = [
        f"| Impact | 0.35 | {s5.impact_score} | |",
        f"| Strategic Fit | 0.30 | {s5.strategic_fit_score} | |",
        f"| Feasibility | 0.20 | {s5.feasibility_score} | |",
        f"| Confidence | 0.15 | {s5.confidence_score} | |",
    ]
    scores_table = "\n".join(score_rows)

    # Assumptions table
    if s5.assumptions:
        assumption_rows = "\n".join(
            f"| {a.statement} | {a.severity} | {a.reason} |"
            for a in s5.assumptions
        )
        assumptions_section = f"""## Step 2 — Assumption Classification

| Assumption | Severity | Reason |
|------------|----------|--------|
{assumption_rows}
"""
    else:
        assumptions_section = "## Step 2 — Assumption Classification\n\nNo critical assumptions identified.\n"

    return f"""# Stage 5: Prioritization

**Date:** {date_str}

---

## Step 1 — Scoring

| Dimension | Weight | Score (1–5) | Rationale |
|-----------|--------|-------------|-----------|
{scores_table}

Composite = ({s5.impact_score} × 0.35) + ({s5.strategic_fit_score} × 0.30) + ({s5.feasibility_score} × 0.20) + ({s5.confidence_score} × 0.15) = **{s5.composite_score}**

---

{assumptions_section}
---

## Step 3 — Routing Decision

**Routing: {s5.routing.upper()}**

{s5.rationale}

---

## Step 4 — Decision Record

| Field | Value |
|-------|-------|
| Composite Score | {s5.composite_score} |
| Track | {s5.routing.upper()} |
| Blocking assumptions | {s5.blocking_count} |
| Date | {date_str} |
"""


def _render_s6a(s6a: S6AOutputData, date_str: str) -> str:
    assumptions_list = "\n".join(f"{i+1}. {a}" for i, a in enumerate(s6a.blocking_assumptions_addressed))
    return f"""# Stage 6A: PoC Plan

**Date:** {date_str}

---

## Experiment Goal

{s6a.experiment_goal}

## Assumptions Being Tested

{assumptions_list}

## Experiment Design

{s6a.experiment_design}

## Success Criteria

{s6a.success_criteria}

## Time and Resources

| Item | Value |
|------|-------|
| Duration | {s6a.timeline_weeks} weeks |
| Resources | {s6a.resources_needed} |
"""


def _render_s6b(s6b: S6BOutputData, date_str: str) -> str:
    user_stories = "\n".join(f"- {s}" for s in s6b.user_stories)
    success_metrics = "\n".join(f"- {m}" for m in s6b.success_metrics)
    in_scope = "\n".join(f"- {s}" for s in s6b.in_scope)
    out_of_scope = "\n".join(f"- {s}" for s in s6b.out_of_scope)
    tech_deps = "\n".join(f"- {d}" for d in s6b.technical_dependencies)
    open_qs = "\n".join(f"- {q}" for q in s6b.open_questions)
    risks = "\n".join(f"- {r}" for r in s6b.risks)

    c = s6b.completeness
    return f"""# Stage 6B: PRD

**Date:** {date_str}

---

## Problem Statement

{s6b.problem_statement}

## Target User

{s6b.target_user}

## Success Metrics

{success_metrics}

## User Stories

{user_stories}

## Scope

**In scope:**
{in_scope}

**Out of scope:**
{out_of_scope}

## Technical Dependencies

{tech_deps}

## Open Questions

{open_qs}

## Risks

{risks}

---

## Completeness Check — {c.score}/12

| Check | Status |
|-------|--------|
| Problem statement | {"✓" if c.problem_statement else "✗"} |
| Target user | {"✓" if c.target_user else "✗"} |
| Hypothesis | {"✓" if c.hypothesis else "✗"} |
| Success metrics (≥2) | {"✓" if c.success_metrics else "✗"} |
| User stories (≥3) | {"✓" if c.user_stories else "✗"} |
| In-scope list | {"✓" if c.in_scope else "✗"} |
| Out-of-scope list (≥2) | {"✓" if c.out_of_scope else "✗"} |
| Technical dependencies | {"✓" if c.technical_dependencies else "✗"} |
| Open questions | {"✓" if c.open_questions else "✗"} |
| Non-goals | {"✓" if c.non_goals else "✗"} |
| Rollout phases | {"✓" if c.rollout_phases else "✗"} |
| Risks | {"✓" if c.risks else "✗"} |
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
        (run_dir / "s4-evaluation-rubric-score.md").write_text(_render_s4_rubric(s4, date_str), encoding="utf-8")
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
