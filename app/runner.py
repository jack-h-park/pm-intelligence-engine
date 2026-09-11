"""Pipeline flow planner — the single source of "what runs when, and where it pauses".

Today that logic is smeared across four files as if-elif chains keyed by depth and
gate: ``runs.py::_continue_after_direction`` (archive/note/structure/evaluate/decide
branching), ``approvals.py::_execute_s5_to_s7`` (Gate 2 → S5 → Gate 3),
``routing_review.py::_execute_s6_s7_with_routing`` (S6a/S6b → S7). This module
replaces all of it with one pure, registry-driven planner: given where a run *is*
and where it is *going*, return the stages to run next and whether it then pauses at
a gate or completes.

Model (docs/WORKFLOW_MODEL_REDESIGN.md):
  - A run advances along the linear path S1→S2→S3→S4→S5→(S6a|S6b)→S7.
  - ``target`` is the position it is going to (derived from the chosen depth).
  - A **gate** (registry ``pause``) fires when the run *passes through* that
    position on the way to a deeper target — never when the run *stops at* it
    (so ``evaluate`` stops at S4 with no Gate 2; ``decide`` passes S4 → Gate 2).
  - ``note`` stops at S2 (clean model — no S7 jump; the S2 insight memo is the
    artifact).

This planner is pure and side-effect free; the executor that runs stages and emits
gate notifications consumes its plan.
"""

from __future__ import annotations

from dataclasses import dataclass

from app import pipeline

# The linear spine, excluding the S6 branch (resolved by routing below).
_SPINE = ("s1", "s2", "s3", "s4", "s5", "s6", "s7")


def _resolve_s6(routing: str | None) -> str | None:
    """The concrete S6 position for a routing, or None if not yet routed."""
    return {"poc": "s6a", "prd": "s6b"}.get(routing or "")


def _path(routing: str | None) -> tuple[str, ...]:
    """The concrete position path for a run, with the S6 branch resolved."""
    six = _resolve_s6(routing)
    return tuple(p if p != "s6" else (six or "s6") for p in _SPINE)


def _index(path: tuple[str, ...], position: str) -> int:
    # s6a/s6b both live at the "s6" slot when routing is unresolved.
    if position in ("s6a", "s6b") and "s6" in path:
        return path.index("s6")
    return path.index(position)


@dataclass(frozen=True)
class AdvancePlan:
    """The next segment of a run's journey.

    ``run`` — positions to execute now, in order (may be empty).
    ``then`` — ``"pause"`` (stop at a gate, await a human decision) or
               ``"complete"`` (finalize the run).
    ``at`` — the gate position paused at, or the position completed at.
    """

    run: tuple[str, ...]
    then: str  # "pause" | "complete"
    at: str


def target_for_depth(depth: str) -> str:
    """The position a run of the given depth is going to."""
    return pipeline.position_for_depth(depth)


def plan_advance(current: str, target: str, routing: str | None = None) -> AdvancePlan:
    """Plan the next segment from ``current`` toward ``target``.

    ``current`` is the furthest position already completed. Returns the stages to
    run before the next stop, and whether that stop is a gate pause or completion.

    Called once per segment: the executor runs the returned stages, then either
    pauses (and calls again after the human decides) or completes. ``routing`` must
    be set before advancing past S5 (it selects the S6 branch); it is not needed
    for earlier segments because a gate always pauses at S5 first.
    """
    path = _path(routing)
    ci = _index(path, current)
    ti = _index(path, target)

    if ti <= ci:
        # Already at/past the target (e.g. archive: S2 ran pre-direction, target
        # is S1). Nothing to run — complete where we are.
        return AdvancePlan(run=(), then="complete", at=current)

    to_run: list[str] = []
    for i in range(ci + 1, ti + 1):
        position = path[i]
        if position == "s6":
            raise ValueError(
                "cannot advance through S6 without a routing (poc/prd) — "
                "the S5 gate must resolve it first"
            )
        to_run.append(position)
        # A gate fires after running a pause-position that we are passing THROUGH
        # (i.e. it is not the target). Stopping at a pause-position completes.
        if pipeline.is_pause(position) and i < ti:
            return AdvancePlan(run=tuple(to_run), then="pause", at=position)

    return AdvancePlan(run=tuple(to_run), then="complete", at=target)


# ---------------------------------------------------------------------------
# Stage executor — the single place that runs a stage by position
# ---------------------------------------------------------------------------


def _load_s5(store, run_id):
    import json

    from app.models.stages import S5OutputData

    raw = store.get_stage_output(run_id, "s5")
    return S5OutputData(**json.loads(raw["output_json"])["output"])


async def run_stage(position: str, run_id: str, engine, context) -> None:
    """Execute one pipeline stage by position, loading its inputs from the store.

    The single home for per-stage I/O, shared by every advance segment. S3/S4 are
    reused when already computed (what makes ``POST /deepen`` cheap); S5–S7 always
    run. S5 additionally records the routing AND the composite score it computes
    onto the run.
    """
    import json

    from app.models.stages import (
        S2OutputData,
        S3Input,
        S3OutputData,
        S4Input,
        S4OutputData,
        S5Input,
        S6AInput,
        S6AOutputData,
        S6BInput,
        S6BOutputData,
        S7Input,
    )

    store = engine.store
    if position in ("s3", "s4") and store.get_stage_output(run_id, position) is not None:
        return  # already computed — deepen reuse

    store.advance(run_id, position)

    if position == "s3":
        s2_raw = store.get_stage_output(run_id, "s2")
        s2_output_data = S2OutputData(**json.loads(s2_raw["output_json"])["output"])
        signal_id = json.loads(s2_raw["output_json"]).get("signal_id", run_id)
        from app.stages import s3_opportunity

        await s3_opportunity.run(
            input=S3Input(
                signal_id=signal_id,
                s2_output=s2_output_data,
                product_id=context.product_id,
            ),
            context=context,
            llm=engine.llm,
            store=store,
        )
    elif position == "s4":
        s3_raw = store.get_stage_output(run_id, "s3")
        s3_output_data = S3OutputData(**json.loads(s3_raw["output_json"])["output"])
        from app.stages import s4_evaluation

        await s4_evaluation.run(
            S4Input(s3_output=s3_output_data),
            context,
            engine.llm,
            store,
        )
    elif position == "s5":
        s4_raw = store.get_stage_output(run_id, "s4")
        s4_output_data = S4OutputData(**json.loads(s4_raw["output_json"])["output"])
        from app.stages import s5_prioritization

        s5_out = await s5_prioritization.run(
            S5Input(s4_output=s4_output_data),
            context,
            engine.llm,
            store,
        )
        # Both halves of S5's verdict, not just the routing. `composite_score` is a
        # column on the run and the only write to it is here, so leaving it out left
        # it NULL on every run that ever reached S5 — while the routing beside it
        # landed, which is what made the gap look like an edge case instead of the
        # whole population. Portfolio synthesis reads it off the run row and had been
        # printing "composite: —" for every product since the column existed.
        store.update_run(
            run_id,
            routing=s5_out.output.routing,
            composite_score=s5_out.output.composite_score,
        )
    elif position == "s6a":
        from app.stages import s6a_poc_plan

        await s6a_poc_plan.run(
            S6AInput(s5_output=_load_s5(store, run_id)),
            context,
            engine.llm,
            store,
        )
    elif position == "s6b":
        from app.stages import s6b_prd

        await s6b_prd.run(
            S6BInput(s5_output=_load_s5(store, run_id)),
            context,
            engine.llm,
            store,
        )
    elif position == "s7":
        s6a_raw = store.get_stage_output(run_id, "s6a")
        s6b_raw = store.get_stage_output(run_id, "s6b")
        s7_in = S7Input(
            mode="decide",
            s5_output=_load_s5(store, run_id),
            s6a_output=(
                S6AOutputData(**json.loads(s6a_raw["output_json"])["output"]) if s6a_raw else None
            ),
            s6b_output=(
                S6BOutputData(**json.loads(s6b_raw["output_json"])["output"]) if s6b_raw else None
            ),
        )
        from app.stages import s7_summary

        await s7_summary.run(s7_in, context, engine.llm, store)
    else:
        raise ValueError(f"run_stage does not handle position {position!r}")
