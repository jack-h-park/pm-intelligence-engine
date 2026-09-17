"""Bounded, hash-addressed context selection for personal insight analysis."""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.models.insights import Candidate, EvidenceBundle, PreparedContext


@dataclass(frozen=True)
class ResolvedInterest:
    """Configured question and guardrails selected by a stable interest ID."""

    id: str
    question: str
    constraints: list[str]
    context_revision: str


def _load_interest_config(decision_context_root: str | Path) -> dict[str, Any]:
    root = Path(decision_context_root)
    config_path = root / "core" / "signal-interest-context.yaml"
    return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}


def resolve_interest(
    question_ids: list[str], decision_context_root: str | Path
) -> ResolvedInterest | None:
    """Resolve the first configured interest, preserving caller ID order."""
    config = _load_interest_config(decision_context_root)
    interests = {item["id"]: item for item in config.get("interests") or []}
    for question_id in question_ids:
        interest = interests.get(question_id)
        if interest is not None:
            return ResolvedInterest(
                id=interest["id"],
                question=interest["question"],
                constraints=list(interest.get("constraints", [])),
                context_revision=str(config.get("revision", "unknown")),
            )
    return None


def load_prepared_context(
    candidate: Candidate, bundle: EvidenceBundle, decision_system_root: str | Path
) -> PreparedContext:
    """Load selected decision assets as data and retain their stable provenance."""
    root = Path(decision_system_root)
    config = _load_interest_config(root)
    interest = resolve_interest(candidate.question_ids, root)
    if candidate.question_ids and interest is None:
        raise ValueError("No configured interest matched the candidate question IDs")
    if interest is None:
        question = candidate.subject
        constraints: list[str] = []
        reasons = ["No configured interest matched the candidate question IDs."]
    else:
        question = interest.question
        constraints = interest.constraints
        reasons = [f"Matched configured interest: {interest.id}"]
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
        question_ids=[interest.id] if interest is not None else [],
        constraints=constraints,
        unresolved_questions=bundle.coverage_gaps,
        validation_status="valid" if bundle.passages else "needs_evidence",
        context_revision=str(config.get("revision", bundle.context_revision)),
        context_paths=paths,
        context_hashes=hashes,
        relevance_reasons=reasons,
        note_connections=[],
    )


__all__ = ["PreparedContext", "ResolvedInterest", "load_prepared_context", "resolve_interest"]
