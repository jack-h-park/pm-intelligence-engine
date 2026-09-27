"""Rebuildable Markdown projection of authoritative insight revisions."""

import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path

from app.models.insights import InsightRevision
from app.storage.insight_store import InsightStore

logger = logging.getLogger(__name__)


def _projection_text(insight: InsightRevision) -> str:
    claims = "\n".join(
        f"- {claim.text} ({', '.join(claim.passage_ids)})" for claim in insight.claims
    )
    uncertainties = "\n".join(f"- {item}" for item in insight.uncertainties) or "- None recorded."
    return (
        f"# {insight.headline}\n\n"
        f"## Actual change\n\n{insight.actual_change}\n\n"
        f"## Takeaway\n\n{insight.takeaway}\n\n"
        f"## Explanation\n\n{insight.explanation}\n\n"
        f"## Why now\n\n{insight.why_now}\n\n"
        f"## Claims\n\n{claims}\n\n"
        f"## Uncertainties\n\n{uncertainties}\n"
    )


def _projection_path(root: Path, insight_id: str) -> Path:
    if not insight_id or Path(insight_id).name != insight_id or "\\" in insight_id:
        raise ValueError("Insight ID is not a safe projection filename")
    return root / f"{insight_id}.md"


def project_insight(insight: InsightRevision, projection_root: str | Path) -> Path:
    """Atomically replace only this revision's derived Markdown file."""
    root = Path(projection_root)
    destination = _projection_path(root, insight.insight_id)
    root.mkdir(parents=True, exist_ok=True)
    temp = root / f".{insight.insight_id}.tmp"
    temp.write_text(_projection_text(insight), encoding="utf-8")
    os.replace(temp, destination)
    return destination


def reconcile_projections(
    current_insights: list[InsightRevision], projection_root: str | Path
) -> list[Path]:
    """Restore every current derived projection from authoritative stored revisions."""
    return [project_insight(insight, projection_root) for insight in current_insights]


def reconcile_store_projections(store: InsightStore, projection_root: str | Path) -> list[Path]:
    """Rebuild current files and retire only unchanged superseded projections."""
    all_insights = store.list_insights()
    superseded_ids = {insight.supersedes_insight_id for insight in all_insights}
    current = [insight for insight in all_insights if insight.insight_id not in superseded_ids]
    written = reconcile_projections(current, projection_root)
    root = Path(projection_root)
    for old in all_insights:
        if old.insight_id not in superseded_ids:
            continue
        path = _projection_path(root, old.insight_id)
        if path.is_symlink():
            logger.warning("Skipping symlinked superseded Insight projection %s", old.insight_id)
            continue
        if not path.is_file():
            continue
        try:
            if path.read_text(encoding="utf-8") == _projection_text(old):
                path.unlink()
            else:
                logger.warning(
                    "Preserving modified superseded Insight projection %s", old.insight_id
                )
        except OSError:
            # A modified or inaccessible file is not proven to be our projection.
            logger.warning("Could not retire superseded Insight projection %s", old.insight_id)
            continue
    entries = [
        {
            "insight_id": insight.insight_id,
            "revision": insight.revision,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for insight, path in sorted(
            zip(current, written), key=lambda pair: pair[0].insight_id
        )
    ]
    manifest = root / ".projection-manifest.json"
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root, delete=False) as temp:
        try:
            json.dump({"schema_version": 1, "entries": entries}, temp, sort_keys=True)
            temp.write("\n")
            temp.flush()
            os.replace(temp.name, manifest)
        finally:
            if os.path.exists(temp.name):
                os.unlink(temp.name)
    return written
