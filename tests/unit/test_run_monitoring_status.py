from sqlalchemy import text

from app.storage.sqlite_store import SQLiteStore


def test_revision_covers_new_resume_fast_completion_restart_and_rollback(tmp_path):
    url = f"sqlite:///{tmp_path}/monitor.db"
    store = SQLiteStore(url)
    first = store.run_monitoring_status()
    signal = store.save_signal(original_product_id="example", title="Test", raw_content="Body")
    run = store.create_run("example", signal)
    created = store.run_monitoring_status()
    assert created["active_count"] == 1 and created["revision"] > first["revision"]
    with store._engine.begin() as conn:
        conn.execute(
            text("UPDATE workflow_runs SET lifecycle='done', outcome='completed' WHERE run_id=:id"),
            {"id": run},
        )
    done = store.run_monitoring_status()
    assert done["active_count"] == 0 and done["revision"] > created["revision"]
    restarted = SQLiteStore(url)
    assert restarted.run_monitoring_status() == done
    with store._engine.begin() as conn:
        conn.execute(
            text("UPDATE workflow_runs SET lifecycle='paused' WHERE run_id=:id"), {"id": run}
        )
    resumed = store.run_monitoring_status()
    assert resumed["active_count"] == 1 and resumed["revision"] > done["revision"]
    with store._engine.connect() as conn:
        conn.execute(
            text("UPDATE workflow_runs SET lifecycle='done', outcome='completed' WHERE run_id=:id"),
            {"id": run},
        )
        conn.rollback()
    assert store.run_monitoring_status() == resumed


def test_paged_run_inventory_does_not_lose_more_than_fifty_runs(tmp_path):
    store = SQLiteStore(f"sqlite:///{tmp_path}/monitor.db")
    signal = store.save_signal(original_product_id="example", title="Test", raw_content="Body")
    for _ in range(105):
        store.create_run("example", signal)
    ids = [r["run_id"] for start in (0, 50, 100) for r in store.list_runs(limit=50, offset=start)]
    assert len(ids) == len(set(ids)) == 105


def test_existing_database_migration_and_replacement_epoch(tmp_path):
    url = f"sqlite:///{tmp_path}/existing.db"
    store = SQLiteStore(url)
    signal = store.save_signal(original_product_id="example", title="Test", raw_content="Body")
    store.create_run("example", signal)
    with store._engine.begin() as conn:
        for action in ("insert", "update", "delete"):
            conn.execute(text(f"DROP TRIGGER run_monitor_{action}"))
        conn.execute(text("DROP TABLE run_monitor_revision"))
    migrated = SQLiteStore(url).run_monitoring_status()
    assert migrated["active_count"] == 1
    other = SQLiteStore(f"sqlite:///{tmp_path}/replacement.db").run_monitoring_status()
    assert migrated["epoch"] != other["epoch"]
