from app.storage.sqlite_store import SQLiteStore


def test_new_and_historical_runs_default_to_legacy_pipeline(tmp_path):
    store = SQLiteStore(f"sqlite:///{tmp_path}/workflow.db")
    signal_id = store.save_signal("Fixture", "Fixture content", "android-enterprise")

    run_id = store.create_run("android-enterprise", signal_id)

    assert store.get_run(run_id)["decision_pipeline_version"] == "legacy"
