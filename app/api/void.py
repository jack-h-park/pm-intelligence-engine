"""Void Run API — administratively cancel an improperly-started run.

This is the missing exit for runs that should never have started — e.g. a Gate 0
mis-classification that spawned a run for the wrong product, or a duplicate /
erroneous start. Before this endpoint, a run sitting at `waiting_direction` had
**no** clean kill path:
  - `reject` (Gate 2) is `decide`-mode + `waiting_approval` only → 409 here;
  - the only working exit was the `archive` direction, which runs S2 and lands the
    run as `completed`/`mode=archive` — i.e. recorded as legitimate *evaluated
    work*, not a voided run (the 2026-06-21 b906e4d7 incident).

`void` ends the run as `killed` with a reason, recording an approval event with
action `void` and emitting a `voided` run event. It is distinct from:
  - `archive` depth  — `completed`, real (if shallow) evaluated work;
  - Gate 2 `reject`  — a decide-mode evaluation rejection;
  - S5 routing `kill`— a substantive "not worth pursuing" decision.

Usable from any **non-terminal** state (`pending`, `running`, `waiting_direction`,
`waiting_approval`, `waiting_routing_review`); a run that is already terminal
(`completed` / `killed` / `failed`) returns 409.

State transition:
  <any non-terminal> + POST /void { reason } → killed (event=voided)
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_engine
from app.factory import PMEngine

router = APIRouter(prefix="/runs", tags=["void"])

class VoidRequest(BaseModel):
    reason: str


@router.post("/{run_id}/void", status_code=200)
async def void_run(
    run_id: str,
    body: VoidRequest,
    engine: PMEngine = Depends(get_engine),
) -> dict:
    run = engine.store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    # A terminal run (lifecycle=done) has already reached an endpoint (US-55).
    voided_from = f"{run.get('lifecycle')}@{run.get('position')}"
    if run.get("lifecycle") == "done":
        raise HTTPException(
            status_code=409,
            detail=f"Run is already terminal ('{voided_from}'); nothing to void",
        )

    # Record the void as a labeled decision event, distinct from a Gate 2 reject
    # or an S5 routing kill. stage falls back to s1 when the run never advanced.
    engine.store.record_approval(
        run_id=run_id,
        stage=run.get("position") or "s1",
        action="void",
        feedback_text=body.reason,
    )
    # Clear any routing S5 may have set so the run is not finalized as a routing
    # kill — this is an administrative void, not a routing decision.
    engine.store.update_run(run_id, routing=None)

    from app.services.run_finalizer import finalize_run

    await finalize_run(
        run_id,
        "killed",
        engine,
        event_action="voided",
        event_detail={"reason": body.reason, "voided_from": voided_from},
    )

    return {
        "run_id": run_id,
        "action": "voided",
        "voided_from": voided_from,
        "reason": body.reason,
    }
