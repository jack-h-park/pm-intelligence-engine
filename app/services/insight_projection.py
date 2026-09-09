"""Rebuildable Markdown projection of authoritative insight revisions."""

import os
from pathlib import Path

from app.models.insights import InsightRevision


def project_insight(insight: InsightRevision, projection_root: str | Path) -> Path:
    """Atomically replace only this revision's derived Markdown file."""
    root = Path(projection_root)
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"{insight.insight_id}.md"
    temp = root / f".{insight.insight_id}.tmp"
    claims = "\n".join(
        f"- {claim.text} ({', '.join(claim.passage_ids)})" for claim in insight.claims
    )
    uncertainties = "\n".join(f"- {item}" for item in insight.uncertainties) or "- None recorded."
    temp.write_text(
        f"# {insight.headline}\n\n"
        f"## Actual change\n\n{insight.actual_change}\n\n"
        f"## Takeaway\n\n{insight.takeaway}\n\n"
        f"## Explanation\n\n{insight.explanation}\n\n"
        f"## Why now\n\n{insight.why_now}\n\n"
        f"## Claims\n\n{claims}\n\n"
        f"## Uncertainties\n\n{uncertainties}\n",
        encoding="utf-8",
    )
    os.replace(temp, destination)
    return destination
