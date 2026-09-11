"""A depth the system picked is revivable; a depth the PM picked is not.

`gate1-timeout` (an ops cron) advances a run that has waited 24h at Gate 1, using the
same `POST /runs/{id}/decision` a human uses. Until this change it recorded
`action="direction"` — byte-identical to the PM's own answer — with two consequences:

1. `reopen` refused it. Measured against the live engine 2026-08-30: of 13 runs the
   timeout had advanced, **0** were reopen-able, while the message it sends had offered
   `reopen` on all 13.
2. Anything grading Gate 1 agreement counted it as a human decision. Since the job
   advances at S2's *own* suggested depth, every such row agrees by construction — the
   live `with_depth_basis` bucket read 14 gradable at 1.0 where only 1 was a human.

`origin` splits the two apart at the moment the row is written. Absent means a human,
which is every caller that predates the field.

Reopen also matters because it is the ONLY route back to a shallower depth: it clears
the depth and returns the run to Gate 1. `deepen` refuses anything not strictly deeper,
so `structure -> note` is reachable this way or not at all.
"""
# ruff: noqa: F811 — `client`/`engine` are pytest fixtures imported for reuse; every
# test function below legitimately takes them as parameters of the same name, which
# ruff reads as shadowing the import rather than fixture injection.

import pytest

from tests.integration.conftest import seed_run_state
from tests.integration.test_auto_triage_boundary import (  # noqa: F401 — fixtures
    _start_run_with_s2_score,
    client,
    engine,
)


def _paused_at_gate1(client, engine, suggested="structure"):
    """A run sitting at Gate 1 with a real S2 suggestion, awaiting a depth."""
    return _start_run_with_s2_score(
        client, engine, relevance_score=4, suggested_mode=suggested, force_gate1=True)


def _advanced_by_timeout(client, engine, target="note"):
    """A run the timeout advanced, in the state `reopen` sees it: done/completed.

    The advance itself is real — it goes through the endpoint and records the row this
    change is about. Only the pipeline execution is stubbed: the helper's S2 mock is
    scoped to `/runs/start`, so the stages cannot replay here, and the stages are not
    what this file tests. `seed_run_state` uses the store's own writers to reach the
    terminal state the pipeline would have produced.
    """
    run_id = _paused_at_gate1(client, engine)
    resp = client.post(f"/runs/{run_id}/decision",
                       json={"action": "advance_to", "target": target,
                             "origin": "gate1-timeout"})
    assert resp.status_code == 202, resp.text
    seed_run_state(engine.store, run_id, "completed", mode=target)
    return run_id


# ── the row the timeout writes ───────────────────────────────────────────────

def test_a_timeout_advance_is_recorded_as_its_own_action(client, engine):
    run_id = _paused_at_gate1(client, engine)

    resp = client.post(f"/runs/{run_id}/decision",
                       json={"action": "advance_to", "target": "note",
                             "origin": "gate1-timeout"})
    assert resp.status_code == 202, resp.text

    actions = [e["action"] for e in engine.store.get_approval_events(run_id)]
    assert "timeout" in actions
    assert "direction" not in actions, (
        "a timeout advance must not be indistinguishable from the PM's own answer")


def test_the_origin_is_kept_in_the_feedback_text(client, engine):
    """The action says which machine wrote it; the text says which job."""
    run_id = _paused_at_gate1(client, engine)
    client.post(f"/runs/{run_id}/decision",
                json={"action": "advance_to", "target": "note", "origin": "gate1-timeout"})

    row = [e for e in engine.store.get_approval_events(run_id) if e["action"] == "timeout"][0]
    assert "origin=gate1-timeout" in row["feedback_text"]
    assert "chose=note" in row["feedback_text"]


def test_a_human_answer_is_unchanged(client, engine):
    """Every existing caller omits `origin`, and must keep behaving exactly as before."""
    run_id = _paused_at_gate1(client, engine)
    client.post(f"/runs/{run_id}/decision", json={"action": "advance_to", "target": "note"})

    actions = [e["action"] for e in engine.store.get_approval_events(run_id)]
    assert "direction" in actions
    assert "timeout" not in actions


def test_an_unknown_origin_is_refused_rather_than_treated_as_human(client, engine):
    """Failing toward 'a human decided it' is the flattering direction: it would restore
    both defects silently. A typo or a version skew has to be loud."""
    run_id = _paused_at_gate1(client, engine)
    resp = client.post(f"/runs/{run_id}/decision",
                       json={"action": "advance_to", "target": "note",
                             "origin": "gate1-timeoutt"})

    assert resp.status_code == 422
    assert "Unknown origin" in resp.text
    assert engine.store.get_approval_events(run_id) == [], "nothing may be recorded"


# ── what reopen now accepts, and what it still refuses ───────────────────────

def test_reopen_revives_a_timeout_advanced_run(client, engine):
    run_id = _advanced_by_timeout(client, engine)

    resp = client.post(f"/runs/{run_id}/reopen")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["lifecycle"] == "paused" and body["position"] == "s2"
    assert body.get("depth") is None, "reopen clears the depth — that is the undo"


def test_reopen_is_the_route_back_to_a_shallower_depth(client, engine):
    """The point of the whole change. `deepen` refuses anything not strictly deeper, so
    a run the system advanced to `structure` can only reach `note` by returning to the
    gate first."""
    run_id = _advanced_by_timeout(client, engine, target="structure")

    # Going shallower directly is refused, and stays refused.
    assert client.post(f"/runs/{run_id}/deepen", json={"depth": "note"}).status_code == 409

    assert client.post(f"/runs/{run_id}/reopen").status_code == 200
    resp = client.post(f"/runs/{run_id}/decision",
                       json={"action": "advance_to", "target": "note"})
    assert resp.status_code == 202, resp.text
    assert engine.store.get_run(run_id)["mode"] == "note"


def test_reopen_still_refuses_a_pm_decision(client, engine):
    """The boundary this endpoint exists to hold. Widening it to automated origins must
    not widen it to the PM's own answer."""
    run_id = _paused_at_gate1(client, engine)
    client.post(f"/runs/{run_id}/decision", json={"action": "advance_to", "target": "note"})

    resp = client.post(f"/runs/{run_id}/reopen")
    assert resp.status_code == 409
    assert "PM decision" in resp.text


def test_reopen_still_revives_an_auto_triaged_run(client, engine):
    """The original member, unaffected."""
    run_id = _start_run_with_s2_score(client, engine, relevance_score=2,
                                      suggested_mode="file")
    assert client.post(f"/runs/{run_id}/reopen").status_code == 200


def test_reopen_twice_is_still_refused(client, engine):
    run_id = _advanced_by_timeout(client, engine)
    assert client.post(f"/runs/{run_id}/reopen").status_code == 200
    assert client.post(f"/runs/{run_id}/reopen").status_code == 409


# ── the new action is a first-class event ────────────────────────────────────

def test_timeout_is_queryable_as_an_event(client, engine):
    """`GET /runs?event=` validates against a fixed set; a new action that is not in it
    is unqueryable, which would hide exactly the rows an audit needs."""
    run_id = _advanced_by_timeout(client, engine)

    resp = client.get("/runs", params={"event": "timeout"})
    assert resp.status_code == 200, resp.text
    assert any(r["run_id"] == run_id for r in resp.json())


@pytest.mark.parametrize("action", ["auto_triaged", "timeout"])
def test_every_revivable_action_is_a_valid_event_filter(client, action):
    """The two lists are in different files and a drift between them is silent."""
    assert client.get("/runs", params={"event": action}).status_code == 200
