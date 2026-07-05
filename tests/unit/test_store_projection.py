"""The store physically persists the (position, lifecycle) columns (US-55 step 6).

Step 5 derived these fields at the API layer; step 6 dual-writes them as real
columns so the direct-SQLite reader (observatory) can query them. These tests
assert the columns are actually stored (raw SQL), stay consistent across
transitions, and — the key new behavior — that `position` is NOT cleared on
finalize the way `current_stage` is.
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
    store.update_run(run_id, status="waiting_approval", current_stage="s4", mode="decide")
    raw = _raw(store, run_id)
    assert raw["lifecycle"] == "paused"
    assert raw["position"] == "s4"
    assert raw["outcome"] is None


def test_completed_persists_position_after_finalize(store):
    """current_stage is cleared on finalize; position must survive (= target)."""
    run_id = _seed(store)
    store.update_run(run_id, status="running", current_stage="s7", mode="decide")
    store.update_run(run_id, status="completed", current_stage=None)  # finalize clears stage

    raw = _raw(store, run_id)
    assert raw["lifecycle"] == "done"
    assert raw["outcome"] == "completed"
    assert raw["position"] == "s7"      # inferred from target, NOT null
    assert raw["reason"] is None
    # current_stage really is cleared, position really is not:
    assert store.get_run(run_id)["current_stage"] is None
    assert store.get_run(run_id)["position"] == "s7"


def test_killed_records_stopped_outcome_and_reason(store):
    run_id = _seed(store)
    store.update_run(run_id, status="waiting_approval", current_stage="s4", mode="decide")
    store.update_run(run_id, status="killed", current_stage=None, ended_by="rejected")
    raw = _raw(store, run_id)
    assert raw["lifecycle"] == "done"
    assert raw["outcome"] == "stopped"
    assert raw["reason"] == "rejected"


def test_archive_completion(store):
    run_id = _seed(store)
    store.update_run(run_id, status="completed", mode="archive", ended_by="archived")
    raw = _raw(store, run_id)
    assert raw["lifecycle"] == "done"
    assert raw["outcome"] == "completed"
    assert raw["position"] == "s1"  # archive stops at s1


def test_serializer_exposes_columns(store):
    run_id = _seed(store)
    store.update_run(run_id, status="waiting_routing_review", current_stage="s5", mode="decide")
    d = store.get_run(run_id)
    assert d["lifecycle"] == "paused"
    assert d["position"] == "s5"
    assert d["outcome"] is None
    assert d["reason"] is None


# --- step 7b: advance()/pause() write (lifecycle, position) as authority --------
# The non-terminal transitions now go through advance()/pause() instead of
# update_run(status=, current_stage=). These assert the NEW columns are the
# authoritative write and status/current_stage are derived to the SAME values the
# legacy form produced (so readers + ?status= are unaffected until 7c/7d).

def test_advance_sets_state_and_derives_legacy(store):
    run_id = _seed(store)
    store.advance(run_id, "s5")
    d = store.get_run(run_id)
    assert (d["lifecycle"], d["position"]) == ("running", "s5")   # authoritative
    assert (d["status"], d["current_stage"]) == ("running", "s5")  # derived compat


def test_pause_derives_gate_status(store):
    run_id = _seed(store)
    store.pause(run_id, "s4")
    d = store.get_run(run_id)
    assert (d["lifecycle"], d["position"]) == ("paused", "s4")
    assert d["status"] == "waiting_approval"   # s4 gate → derived compat status
    assert d["current_stage"] == "s4"


def test_advance_passes_through_mode(store):
    run_id = _seed(store)
    store.advance(run_id, "s2", mode="decide")   # Gate-1 resume form
    d = store.get_run(run_id)
    assert d["mode"] == "decide"
    assert (d["lifecycle"], d["position"], d["status"]) == ("running", "s2", "running")


def test_advance_pause_match_legacy_update_run(store):
    """advance/pause must be a drop-in for the update_run(status=) they replaced."""
    a = _seed(store)
    store.pause(a, "s4")
    b = _seed(store)
    store.update_run(b, status="waiting_approval", current_stage="s4")
    da, db = store.get_run(a), store.get_run(b)
    for k in ("status", "current_stage", "lifecycle", "position", "outcome", "reason"):
        assert da[k] == db[k], f"mismatch on {k}: advance/pause={da[k]} legacy={db[k]}"


# --- step 7b-2: finish() is the terminal write, matches the legacy form ---------

def test_finish_completed_matches_legacy(store):
    a = _seed(store)
    store.update_run(a, mode="decide")
    store.finish(a, "completed", position="s7", reason=None, ended_by="decided")
    b = _seed(store)
    store.update_run(b, mode="decide")
    store.update_run(b, status="completed", current_stage=None, ended_by="decided")
    da, db = store.get_run(a), store.get_run(b)
    for k in ("status", "current_stage", "lifecycle", "position", "outcome", "reason", "ended_by"):
        assert da[k] == db[k], f"mismatch on {k}: finish={da[k]} legacy={db[k]}"
    assert da["status"] == "completed" and da["completed_at"] is not None


def test_finish_killed_records_reason(store):
    run_id = _seed(store)
    store.pause(run_id, "s4")
    store.finish(run_id, "stopped", position=None, reason="rejected", ended_by="rejected")
    d = store.get_run(run_id)
    assert (d["status"], d["lifecycle"], d["outcome"], d["reason"]) == (
        "killed", "done", "stopped", "rejected")
    assert d["completed_at"] is not None


def test_finish_failed_no_completed_at(store):
    run_id = _seed(store)
    store.advance(run_id, "s3")
    store.finish(run_id, "failed", position="s3", reason="boom",
                 ended_by="failed", failed_stage="s3", error="boom")
    d = store.get_run(run_id)
    assert (d["status"], d["outcome"], d["position"], d["reason"]) == (
        "failed", "failed", "s3", "boom")
    assert d["failed_stage"] == "s3" and d["error"] == "boom"
    assert d["completed_at"] is None      # failed never gets completed_at


# --- step 7c: list_runs filters on (lifecycle, position), == the status filter ---

def test_list_runs_lifecycle_position_filter(store):
    g2 = _seed(store); store.pause(g2, "s4")           # Gate 2
    g3 = _seed(store); store.pause(g3, "s5")           # Gate 3
    live = _seed(store); store.advance(live, "s3")     # running

    # Gate 2 = paused@s4 — the new filter picks exactly the same run as ?status=.
    by_state = store.list_runs(lifecycle="paused", position="s4")
    by_status = store.list_runs(status="waiting_approval")
    assert [r["run_id"] for r in by_state] == [r["run_id"] for r in by_status] == [g2]

    # lifecycle alone spans both gates; running is separate.
    assert {r["run_id"] for r in store.list_runs(lifecycle="paused")} == {g2, g3}
    assert [r["run_id"] for r in store.list_runs(lifecycle="running")] == [live]


def test_list_runs_outcome_filter_distinguishes_terminals(store):
    """?lifecycle=done alone spans all terminals; outcome splits them. Without the
    outcome filter, the gate-watcher's three terminal queries each returned every
    done run (each run keyed completed AND killed AND failed) — the 7c-2 deploy bug."""
    done_c = _seed(store); store.finish(done_c, "completed", position="s7")
    done_k = _seed(store); store.finish(done_k, "stopped", position=None)
    done_f = _seed(store); store.finish(done_f, "failed", position="s3")

    assert {r["run_id"] for r in store.list_runs(lifecycle="done")} == {done_c, done_k, done_f}
    assert [r["run_id"] for r in store.list_runs(lifecycle="done", outcome="completed")] == [done_c]
    assert [r["run_id"] for r in store.list_runs(lifecycle="done", outcome="stopped")] == [done_k]
    assert [r["run_id"] for r in store.list_runs(lifecycle="done", outcome="failed")] == [done_f]


def test_init_backfills_legacy_null_columns(store, tmp_path):
    """A row written before dual-write has NULL canonical columns; re-opening the
    store must backfill them so the ?lifecycle= filter sees the row (US-55 7c)."""
    run_id = _seed(store)
    store.pause(run_id, "s4")
    # Simulate a legacy row: NULL the canonical columns behind the store's back.
    with store._engine.begin() as conn:
        conn.execute(
            text("UPDATE workflow_runs SET lifecycle=NULL, position=NULL, "
                 "outcome=NULL, reason=NULL WHERE run_id=:r"),
            {"r": run_id},
        )
    assert _raw(store, run_id)["lifecycle"] is None
    assert store.list_runs(lifecycle="paused") == []   # invisible while NULL

    reopened = SQLiteStore(f"sqlite:///{tmp_path}/test.db")   # _migrate → backfill
    assert _raw(reopened, run_id) == {
        "lifecycle": "paused", "position": "s4", "outcome": None, "reason": None,
    }
    assert [r["run_id"] for r in reopened.list_runs(lifecycle="paused")] == [run_id]
