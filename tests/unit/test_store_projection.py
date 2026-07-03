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
