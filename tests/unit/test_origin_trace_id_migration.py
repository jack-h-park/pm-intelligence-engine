"""The `origin_trace_id` ADD COLUMN has to survive a database that already exists.

Every other column on `workflow_runs` arrived the same way — a guarded
`ALTER TABLE ... ADD COLUMN` in `SQLiteStore._migrate` — and none of them has a
test. The upgrade is the only part of this change that touches production data:
the ops host's `pm_platform.db` carries real runs, the store migrates on every
construction, and a migration that is not idempotent fails on the SECOND start,
not the first, which is the start nobody is watching.

So this drives the real upgrade path rather than asserting the DDL string
exists: build a database, remove the column to recreate the pre-upgrade shape,
then re-open the store exactly as startup does.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from app.storage.sqlite_store import SQLiteStore


def _columns(url: str) -> set[str]:
    return {c["name"] for c in inspect(create_engine(url)).get_columns("workflow_runs")}


@pytest.fixture()
def legacy_db(tmp_path):
    """A database holding a run, shaped as it was before this column existed."""
    url = f"sqlite:///{tmp_path}/legacy.db"
    store = SQLiteStore(url)
    signal_id = store.save_signal(title="t", raw_content="c", source_type="rss")
    run_id = store.create_run(product_id="prod-a", signal_id=signal_id)

    engine = create_engine(url)
    with engine.begin() as conn:
        # SQLite >= 3.35. If the runner's build is older the premise of the test
        # cannot be set up, and passing would be meaningless.
        try:
            conn.execute(text("ALTER TABLE workflow_runs DROP COLUMN origin_trace_id"))
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"cannot build the pre-upgrade shape: {exc}")
    engine.dispose()
    assert "origin_trace_id" not in _columns(url)
    return url, run_id


def test_upgrading_an_existing_database_adds_the_column(legacy_db):
    url, run_id = legacy_db

    run = SQLiteStore(url).get_run(run_id)

    assert "origin_trace_id" in _columns(url)
    # The pre-existing run reads as "started before the link existed" — not as a
    # missing key, and not as "".
    assert run is not None and run["origin_trace_id"] is None


def test_migrating_twice_is_a_no_op(legacy_db):
    """Construction runs the migration, and the service constructs the store on
    every boot. A second ADD COLUMN would raise and take startup down."""
    url, run_id = legacy_db

    SQLiteStore(url)
    store = SQLiteStore(url)  # the next restart

    assert store.get_run(run_id) is not None


def test_the_column_survives_alongside_rows_written_before_it(legacy_db):
    """A new traced run and an old untraced one must coexist and stay
    distinguishable — the upgrade must not backfill a value onto history."""
    url, old_run_id = legacy_db
    store = SQLiteStore(url)

    signal_id = store.save_signal(title="t2", raw_content="c2", source_type="rss")
    new_run_id = store.create_run(
        product_id="prod-a", signal_id=signal_id, origin_trace_id="20260920_143001_abc"
    )

    assert store.get_run(old_run_id)["origin_trace_id"] is None
    assert store.get_run(new_run_id)["origin_trace_id"] == "20260920_143001_abc"
