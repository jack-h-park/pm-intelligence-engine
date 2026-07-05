"""The store persists the canonical (lifecycle, position, outcome, reason) columns
as the AUTHORITATIVE run state (US-55 step 7d-1).

advance()/pause()/finish() are the only writers of run state; the legacy
status/current_stage columns are no longer read or written by the engine. These
tests assert the canonical columns are stored (raw SQL, as the observatory reads),
stay consistent across transitions, and that `position` survives finalize.
"""

import pytest
from sqlalchemy import text

from app.storage.sqlite_store import SQLiteStore


@pytest.fixture()
def store(tmp_path):
    return SQLiteStore(f"sqlite:///{tmp_path}/test.db")


def _seed(store: SQLiteStore) -> str:
    signal_id = store.save_signal(
        original_product_id="example-security-product", title="Sig", raw_content="Body.",
    )
    return store.create_run("example-security-product", signal_id)


def _raw(store: SQLiteStore, run_id: str) -> dict:
    """Read the canonical columns straight from SQLite (as the observatory does)."""
    with store._engine.connect() as conn:
        row = conn.execute(
            text("SELECT lifecycle, position, outcome, reason FROM workflow_runs WHERE run_id=:r"),
            {"r": run_id},
        ).mappings().one()
    return dict(row)


def test_new_run_is_running(store):
    run_id = _seed(store)
    assert _raw(store, run_id) == {
        "lifecycle": "running", "position": None, "outcome": None, "reason": None,
    }


def test_paused_at_gate2(store):
    run_id = _seed(store)
    store.pause(run_id, "s4", mode="decide")
    raw = _raw(store, run_id)
    assert raw["lifecycle"] == "paused"
    assert raw["position"] == "s4"
    assert raw["outcome"] is None


def test_completed_persists_position_after_finalize(store):
    """position must survive finalize (= target); it is not cleared."""
    run_id = _seed(store)
    store.update_run(run_id, mode="decide")
    store.advance(run_id, "s7")
    store.finish(run_id, "completed", position="s7")

    raw = _raw(store, run_id)
    assert raw["lifecycle"] == "done"
    assert raw["outcome"] == "completed"
    assert raw["position"] == "s7"      # target, NOT null
    assert raw["reason"] is None
    assert store.get_run(run_id)["position"] == "s7"


def test_killed_records_stopped_outcome_and_reason(store):
    run_id = _seed(store)
    store.pause(run_id, "s4", mode="decide")
    store.finish(run_id, "stopped", position=None, reason="rejected")
    raw = _raw(store, run_id)
    assert raw["lifecycle"] == "done"
    assert raw["outcome"] == "stopped"
    assert raw["reason"] == "rejected"


def test_archive_completion(store):
    run_id = _seed(store)
    store.update_run(run_id, mode="archive")
    store.finish(run_id, "completed", position="s1")
    raw = _raw(store, run_id)
    assert raw["lifecycle"] == "done"
    assert raw["outcome"] == "completed"
    assert raw["position"] == "s1"  # archive stops at s1


def test_serializer_exposes_columns(store):
    run_id = _seed(store)
    store.pause(run_id, "s5", mode="decide")
    d = store.get_run(run_id)
    assert d["lifecycle"] == "paused"
    assert d["position"] == "s5"
    assert d["outcome"] is None
    assert d["reason"] is None
    # status/current_stage are no longer surfaced by the serializer (US-55 7d-1).
    assert "status" not in d
    assert "current_stage" not in d


# --- advance()/pause() write (lifecycle, position) as authority -----------------

def test_advance_sets_state(store):
    run_id = _seed(store)
    store.advance(run_id, "s5")
    d = store.get_run(run_id)
    assert (d["lifecycle"], d["position"]) == ("running", "s5")


def test_pause_sets_gate_state(store):
    run_id = _seed(store)
    store.pause(run_id, "s4")
    d = store.get_run(run_id)
    assert (d["lifecycle"], d["position"]) == ("paused", "s4")


def test_advance_passes_through_mode(store):
    run_id = _seed(store)
    store.advance(run_id, "s2", mode="decide")   # Gate-1 resume form
    d = store.get_run(run_id)
    assert d["mode"] == "decide"
    assert (d["lifecycle"], d["position"]) == ("running", "s2")


# --- finish() is the terminal write ---------------------------------------------

def test_finish_completed_stamps_completed_at(store):
    run_id = _seed(store)
    store.update_run(run_id, mode="decide")
    store.finish(run_id, "completed", position="s7", reason=None)
    d = store.get_run(run_id)
    assert (d["lifecycle"], d["position"], d["outcome"], d["reason"]) == (
        "done", "s7", "completed", None)
    assert d["completed_at"] is not None


def test_finish_stopped_records_reason(store):
    run_id = _seed(store)
    store.pause(run_id, "s4")
    store.finish(run_id, "stopped", position=None, reason="rejected")
    d = store.get_run(run_id)
    assert (d["lifecycle"], d["outcome"], d["reason"]) == ("done", "stopped", "rejected")
    assert d["completed_at"] is not None


def test_finish_failed_no_completed_at(store):
    run_id = _seed(store)
    store.advance(run_id, "s3")
    store.finish(run_id, "failed", position="s3", reason="boom")
    d = store.get_run(run_id)
    # The failure stage → position, the error → reason (7d-3 dropped failed_stage/error).
    assert (d["outcome"], d["position"], d["reason"]) == ("failed", "s3", "boom")
    assert d["completed_at"] is None      # failed never gets completed_at


# --- list_runs filters on (lifecycle, position, outcome) ------------------------

def test_list_runs_lifecycle_position_filter(store):
    g2 = _seed(store); store.pause(g2, "s4")           # Gate 2
    g3 = _seed(store); store.pause(g3, "s5")           # Gate 3
    live = _seed(store); store.advance(live, "s3")     # running

    by_state = store.list_runs(lifecycle="paused", position="s4")
    assert [r["run_id"] for r in by_state] == [g2]

    # lifecycle alone spans both gates; running is separate.
    assert {r["run_id"] for r in store.list_runs(lifecycle="paused")} == {g2, g3}
    assert [r["run_id"] for r in store.list_runs(lifecycle="running")] == [live]


def test_list_runs_outcome_filter_distinguishes_terminals(store):
    done_c = _seed(store); store.finish(done_c, "completed", position="s7")
    done_k = _seed(store); store.finish(done_k, "stopped", position=None)
    done_f = _seed(store); store.finish(done_f, "failed", position="s3")

    assert {r["run_id"] for r in store.list_runs(lifecycle="done")} == {done_c, done_k, done_f}
    assert [r["run_id"] for r in store.list_runs(lifecycle="done", outcome="completed")] == [done_c]
    assert [r["run_id"] for r in store.list_runs(lifecycle="done", outcome="stopped")] == [done_k]
    assert [r["run_id"] for r in store.list_runs(lifecycle="done", outcome="failed")] == [done_f]


# --- RunResponse reads the canonical columns straight from the row --------------

def test_run_response_reads_canonical_columns(store):
    """The API model surfaces lifecycle/position/outcome/reason from the row
    directly (no status-based derivation; US-55 step 7d-1)."""
    from app.api.runs import RunResponse

    row = {
        "run_id": "r0", "product_id": "p", "signal_id": "s",
        "mode": "decide",
        "recommendation_json": None, "routing": None, "composite_score": None,
        "created_at": "2026-07-03T00:00:00", "completed_at": "2026-07-03T01:00:00",
        "lifecycle": "done", "position": "s7", "outcome": "completed", "reason": None,
    }
    resp = RunResponse(**row)
    assert resp.lifecycle == "done"
    assert resp.position == "s7"
    assert resp.outcome == "completed"
    assert resp.depth == "decide"
    assert not hasattr(resp, "target")   # target is not surfaced (US-55; = depth 1:1)
