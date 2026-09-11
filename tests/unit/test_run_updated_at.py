"""run.updated_at is exposed and bumps on every change (gate-watcher dedup support)."""
import time

from app.storage.sqlite_store import SQLiteStore


def _store(tmp_path):
    return SQLiteStore(f"sqlite:///{tmp_path}/t.db")


def test_updated_at_present_on_create(tmp_path):
    s = _store(tmp_path)
    sid = s.save_signal(original_product_id="p", title="t", raw_content="r")
    rid = s.create_run("p", sid)
    run = s.get_run(rid)
    assert run["updated_at"] is not None
    assert run["updated_at"] >= run["created_at"]


def test_updated_at_bumps_on_update(tmp_path):
    s = _store(tmp_path)
    sid = s.save_signal(original_product_id="p", title="t", raw_content="r")
    rid = s.create_run("p", sid)
    first = s.get_run(rid)["updated_at"]
    time.sleep(0.01)
    s.pause(rid, "s2")  # Gate 1
    second = s.get_run(rid)["updated_at"]
    assert second > first  # re-entering a state is detectable

    time.sleep(0.01)
    s.advance(rid, "s3")
    third = s.get_run(rid)["updated_at"]
    assert third > second
