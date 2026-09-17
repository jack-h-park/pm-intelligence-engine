"""Bounded, hash-addressed context selection for personal insight analysis."""

import hashlib
from pathlib import Path

import yaml

from app.models.insights import Candidate, EvidenceBundle, PreparedContext


def load_prepared_context(
    candidate: Candidate, bundle: EvidenceBundle, decision_system_root: str | Path
) -> PreparedContext:
    """Load selected decision assets as data and retain their stable provenance."""
    root = Path(decision_system_root)
    config_path = root / "core" / "signal-interest-context.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    interests = {item["id"]: item for item in config.get("interests", [])}
    interest = next((interests[key] for key in candidate.question_ids if key in interests), None)
    if candidate.question_ids and interest is None:
        raise ValueError("No configured interest matched the candidate question IDs")
    if interest is None:
        question = candidate.subject
        constraints: list[str] = []
        reasons = ["No configured interest matched the candidate question IDs."]
    else:
        question = interest["question"]
        constraints = list(interest.get("constraints", []))
        reasons = [f"Matched configured interest: {interest['id']}"]
    paths: list[str] = []
    hashes: dict[str, str] = {}
    for relative in config.get("selection", {}).get("identity_paths", []):
        path = root / relative
        if not path.is_file():
            continue
        data = path.read_bytes()
        paths.append(relative)
        hashes[relative] = hashlib.sha256(data).hexdigest()
    return PreparedContext(
        candidate_id=candidate.candidate_id,
        bundle_id=bundle.bundle_id,
        question=question,
        question_ids=[interest["id"]] if interest is not None else [],
        constraints=constraints,
        unresolved_questions=bundle.coverage_gaps,
        validation_status="valid" if bundle.passages else "needs_evidence",
        context_revision=str(config.get("revision", bundle.context_revision)),
        context_paths=paths,
        context_hashes=hashes,
        relevance_reasons=reasons,
        note_connections=[],
    )


__all__ = ["PreparedContext", "load_prepared_context"]
