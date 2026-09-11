"""Stage registry — the single source of truth for the pipeline's positions.

Each pipeline position is defined exactly once here: its human name, the artifact
it produces, whether it is a valid stop target (a "depth"), the terminal reason a
run completing there records, and whether it is a human checkpoint (gate).

This is the foundation of the (position, lifecycle) workflow model
(docs/WORKFLOW_MODEL_REDESIGN.md §2.5). Historically the same position was
re-encoded by several parallel vocabularies — `RunMode`/depth (`structure`),
`ArtifactType` (`opportunity_memo`), and `ended_by` (`structured`) all name S3.
Those stop being independent definitions and become **derivations** of this table.

Positions
---------
``s1 → s2 → s3 → s4 → s5 → (s6a | s6b) → s7``. ``s6`` branches: a PoC run takes
``s6a`` (poc_plan), a PRD run takes ``s6b`` (prd). Only the five *stop* positions
(s1–s4, s7) are valid depths; s5/s6a/s6b are intermediate steps of a ``decide``
run, never a stop point — so they carry no depth and no completion ``ended_by``.

This registry lists the primary rendered artifact per position.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Stage:
    """One pipeline position and everything derivable from it."""

    id: str  # position id: s1..s7 (s6 branches to s6a/s6b)
    name: str  # human display name ("Insight Extraction")
    artifact_label: (
        str | None
    )  # human label for the rendered artifact (None → produces none, e.g. s1)
    artifact_type: str | None  # ArtifactType value produced here (None for s1)
    stop_depth: (
        str | None
    )  # legacy depth name if this is a valid stop target, else None (intermediate)
    ended_by: str | None  # terminal reason when a run COMPLETES here (only stop positions)
    pause: bool  # is there a human checkpoint (gate) after this stage?


# Ordered by pipeline progression. The order of the *stop* rows also defines the
# increasing-depth order (archive < note < structure < evaluate < decide).
STAGES: tuple[Stage, ...] = (
    Stage("s1", "Signal Ingestion", None, None, "archive", "archived", False),
    Stage("s2", "Insight Extraction", "Insight Memo", "insight_memo", "note", "noted", True),
    Stage(
        "s3",
        "Opportunity Creation",
        "Opportunity Memo",
        "opportunity_memo",
        "structure",
        "structured",
        False,
    ),
    Stage(
        "s4",
        "Persona Evaluation",
        "Evaluation Brief",
        "evaluation_brief",
        "evaluate",
        "evaluated",
        True,
    ),
    Stage("s5", "Prioritization & Routing", "Decision Memo", "decision_memo", None, None, True),
    Stage("s6a", "PoC Plan", "PoC Plan", "poc_plan", None, None, False),
    Stage("s6b", "PRD", "PRD", "prd", None, None, False),
    Stage(
        "s7",
        "Executive Summary",
        "Executive Summary",
        "executive_summary",
        "decide",
        "decided",
        False,
    ),
)

_BY_ID: dict[str, Stage] = {s.id: s for s in STAGES}
_BY_DEPTH: dict[str, Stage] = {s.stop_depth: s for s in STAGES if s.stop_depth is not None}


def by_id(stage_id: str) -> Stage:
    """The Stage for a position id. Raises KeyError for an unknown id."""
    return _BY_ID[stage_id]


def depths() -> tuple[str, ...]:
    """The valid stop-depths in increasing-depth order — the canonical ``MODES``."""
    return tuple(s.stop_depth for s in STAGES if s.stop_depth is not None)


def is_depth(value: str) -> bool:
    return value in _BY_DEPTH


def position_for_depth(depth: str) -> str:
    """The position a run stops at for the given depth. KeyError if unknown."""
    return _BY_DEPTH[depth].id


def ended_by_for_depth(depth: str) -> str:
    """Terminal reason recorded when a run completes at the given depth."""
    return _BY_DEPTH[depth].ended_by  # type: ignore[return-value]  # stop rows always set it


def ended_by_by_depth() -> dict[str, str]:
    """``{depth: ended_by}`` for every stop-depth — the canonical completion map."""
    return {s.stop_depth: s.ended_by for s in STAGES if s.stop_depth is not None}  # type: ignore[misc]


def artifact_type_for(stage_id: str) -> str | None:
    """The ArtifactType value produced at a position, or None if it produces none."""
    return _BY_ID[stage_id].artifact_type


def artifact_label_for(stage_id: str) -> str | None:
    return _BY_ID[stage_id].artifact_label


def name_for(stage_id: str) -> str:
    return _BY_ID[stage_id].name


def is_pause(stage_id: str) -> bool:
    """Whether a human checkpoint (gate) follows this stage."""
    return _BY_ID[stage_id].pause


def pause_positions() -> tuple[str, ...]:
    """Positions with a human checkpoint — today's Gate 1 (s2), 2 (s4), 3 (s5)."""
    return tuple(s.id for s in STAGES if s.pause)
