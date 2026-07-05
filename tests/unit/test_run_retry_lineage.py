"""workflow_runs retry lineage & failure diagnostics.

A failed run returns its signal to the retryable pool, and the next pickup
creates a *new* run rather than resuming. attempt_no / root_run_id make that
lineage explicit so retries of one signal can be collapsed to one logical run;
failed_stage / error persist the failure reason in the DB (not just the log).
"""
import sqlalchemy as sa
from app.storage.sqlite_store import SQLiteStore


def _store(tmp_path):
    return SQLiteStore(f"sqlite:///{tmp_path}/t.db")


def test_first_run_is_attempt_one_with_no_root(tmp_path):
    s = _store(tmp_path)
    sid = s.save_signal(title="t", raw_content="c")
    rid = s.create_run(product_id="p1", signal_id=sid)
    run = s.get_run(rid)
    assert run["attempt_no"] == 1
    assert run["root_run_id"] is None  # NULL == self is the lineage root


def test_retry_increments_attempt_and_links_root(tmp_path):
    s = _store(tmp_path)
    sid = s.save_signal(title="t", raw_content="c")
    r1 = s.create_run(product_id="p1", signal_id=sid)
    r2 = s.create_run(product_id="p1", signal_id=sid)
    r3 = s.create_run(product_id="p1", signal_id=sid)
    assert s.get_run(r2)["attempt_no"] == 2
    assert s.get_run(r2)["root_run_id"] == r1
    # The root stays pinned to the first attempt across the whole lineage.
    assert s.get_run(r3)["attempt_no"] == 3
    assert s.get_run(r3)["root_run_id"] == r1


def test_different_product_is_separate_lineage(tmp_path):
    """A fan-out spawns one run per product from one signal — each is its own
    lineage (attempt 1), not a retry of a sibling product's run."""
    s = _store(tmp_path)
    sid = s.save_signal(title="t", raw_content="c")
    s.create_run(product_id="p1", signal_id=sid)
    r_other = s.create_run(product_id="p2", signal_id=sid)
    assert s.get_run(r_other)["attempt_no"] == 1
    assert s.get_run(r_other)["root_run_id"] is None


def test_default_origin_is_start(tmp_path):
    s = _store(tmp_path)
    sid = s.save_signal(title="t", raw_content="c")
    rid = s.create_run(product_id="p1", signal_id=sid)
    assert s.get_run(rid)["origin"] == "start"


def test_refresh_run_starts_fresh_lineage(tmp_path):
    """A refresh (content re-ingest) is NOT "attempt N of N" of the prior
    failure-retry lineage — it resets to attempt 1 / no root and is tagged
    origin='refresh', so the observatory renders it as a re-ingest."""
    s = _store(tmp_path)
    sid = s.save_signal(title="t", raw_content="c")
    s.create_run(product_id="p1", signal_id=sid)            # attempt 1
    r2 = s.create_run(product_id="p1", signal_id=sid)       # attempt 2
    assert s.get_run(r2)["attempt_no"] == 2

    refreshed = s.create_run(product_id="p1", signal_id=sid, origin="refresh")
    run = s.get_run(refreshed)
    assert run["attempt_no"] == 1
    assert run["root_run_id"] is None
    assert run["origin"] == "refresh"


def test_retry_after_refresh_counts_from_the_refresh_boundary(tmp_path):
    """A failure-retry that happens after a refresh continues the refresh
    lineage, not the pre-refresh history."""
    s = _store(tmp_path)
    sid = s.save_signal(title="t", raw_content="c")
    s.create_run(product_id="p1", signal_id=sid)            # pre-refresh attempt 1
    s.create_run(product_id="p1", signal_id=sid)            # pre-refresh attempt 2
    refresh = s.create_run(product_id="p1", signal_id=sid, origin="refresh")  # attempt 1
    retry = s.create_run(product_id="p1", signal_id=sid)    # attempt 2 of refresh lineage
    assert s.get_run(retry)["attempt_no"] == 2
    assert s.get_run(retry)["root_run_id"] == refresh


def test_update_signal_content_overwrites_and_stamps(tmp_path):
    s = _store(tmp_path)
    sid = s.save_signal(title="t", raw_content="thin chrome", category="other")
    assert s.get_signal(sid)["refreshed_at"] is None

    updated = s.update_signal_content(sid, raw_content="full article body", category="platform")
    assert updated["raw_content"] == "full article body"
    assert updated["category"] == "platform"
    assert updated["refreshed_at"] is not None
    # Persisted, not just returned.
    assert s.get_signal(sid)["raw_content"] == "full article body"


def test_update_signal_content_unknown_signal_returns_none(tmp_path):
    s = _store(tmp_path)
    assert s.update_signal_content("nope", raw_content="x") is None


def test_migration_adds_refresh_columns_to_legacy_db(tmp_path):
    """A DB predating the refresh columns gains signals.refreshed_at and
    workflow_runs.origin on next open; existing rows back-fill (origin='start',
    refreshed_at NULL); re-opening is a no-op."""
    url = f"sqlite:///{tmp_path}/legacy.db"
    e = sa.create_engine(url)
    with e.begin() as c:
        c.execute(
            sa.text(
                "CREATE TABLE signals (signal_id VARCHAR PRIMARY KEY, "
                "original_product_id VARCHAR, title VARCHAR, source_url VARCHAR, "
                "source_ref VARCHAR, raw_content TEXT, category VARCHAR, "
                "status VARCHAR, source_type VARCHAR, ingested_at DATETIME)"
            )
        )
        c.execute(
            sa.text(
                "CREATE TABLE workflow_runs (run_id VARCHAR PRIMARY KEY, "
                "product_id VARCHAR, signal_id VARCHAR, status VARCHAR, "
                "current_stage VARCHAR, mode VARCHAR, recommendation_json TEXT, "
                "routing VARCHAR, composite_score FLOAT, created_at DATETIME, "
                "updated_at DATETIME, completed_at DATETIME, batch_id VARCHAR, "
                "prompt_tokens_total INTEGER, completion_tokens_total INTEGER, "
                "attempt_no INTEGER NOT NULL DEFAULT 1, root_run_id VARCHAR, "
                "failed_stage VARCHAR, error TEXT)"
            )
        )
        c.execute(
            sa.text(
                "INSERT INTO signals (signal_id, title, raw_content, category, "
                "status, source_type, ingested_at) VALUES ('s-old', 't', 'c', "
                "'other', 'new', 'manual', '2026-01-01 00:00:00')"
            )
        )
        c.execute(
            sa.text(
                "INSERT INTO workflow_runs (run_id, product_id, signal_id, status, "
                "created_at) VALUES ('r-old', 'p1', 's-old', 'completed', "
                "'2026-01-01 00:00:00')"
            )
        )
    e.dispose()

    store = SQLiteStore(url)
    assert store.get_signal("s-old")["refreshed_at"] is None
    assert store.get_run("r-old")["origin"] == "start"  # back-filled by DEFAULT

    # Idempotent: a second open must not raise.
    reopened = SQLiteStore(url)
    assert reopened.get_run("r-old")["origin"] == "start"


def test_failure_fields_round_trip(tmp_path):
    s = _store(tmp_path)
    sid = s.save_signal(title="t", raw_content="c")
    rid = s.create_run(product_id="p1", signal_id=sid)
    s.finish(rid, "failed", position="s2", reason="boom 400")
    run = s.get_run(rid)
    assert run["outcome"] == "failed"
    # US-55 7d-3: the failure stage → position, the error → reason (the dedicated
    # failed_stage/error columns were dropped).
    assert run["position"] == "s2"
    assert run["reason"] == "boom 400"


def test_migration_adds_lineage_columns_to_legacy_db(tmp_path):
    """A workflow_runs table predating these columns gains them on next open,
    existing rows back-fill (attempt_no=1, the rest NULL), and re-opening is a
    no-op."""
    url = f"sqlite:///{tmp_path}/legacy.db"
    e = sa.create_engine(url)
    with e.begin() as c:
        # Schema as it stood immediately before this change: prior migrations
        # (batch_id, updated_at, token totals) already applied, lineage columns
        # not yet present.
        c.execute(
            sa.text(
                "CREATE TABLE workflow_runs (run_id VARCHAR PRIMARY KEY, "
                "product_id VARCHAR, signal_id VARCHAR, status VARCHAR, "
                "current_stage VARCHAR, mode VARCHAR, recommendation_json TEXT, "
                "routing VARCHAR, composite_score FLOAT, created_at DATETIME, "
                "updated_at DATETIME, completed_at DATETIME, batch_id VARCHAR, "
                "prompt_tokens_total INTEGER, completion_tokens_total INTEGER)"
            )
        )
        c.execute(
            sa.text(
                "INSERT INTO workflow_runs (run_id, product_id, signal_id, status, "
                "created_at) VALUES ('old-run', 'p1', 's1', 'failed', "
                "'2026-01-01 00:00:00')"
            )
        )
    e.dispose()

    store = SQLiteStore(url)
    legacy = store.get_run("old-run")
    assert legacy["attempt_no"] == 1  # back-filled by the column DEFAULT
    assert legacy["root_run_id"] is None
    # failed_stage/error were dropped in 7d-3 — no longer surfaced by the serializer.
    assert "failed_stage" not in legacy
    assert "error" not in legacy

    # New runs over the migrated DB compute lineage normally.
    r2 = store.create_run(product_id="p1", signal_id="s1")
    assert store.get_run(r2)["attempt_no"] == 2
    assert store.get_run(r2)["root_run_id"] == "old-run"

    # Idempotent: a second open must not raise.
    reopened = SQLiteStore(url)
    assert reopened.get_run("old-run")["attempt_no"] == 1


def test_migration_backfills_existing_lineage(tmp_path):
    """The migration reconstructs lineage for pre-existing runs so the fix is
    retroactive: three runs of one signal+product become attempts 1/2/3 sharing
    a root, while a different product's run stays its own attempt 1."""
    url = f"sqlite:///{tmp_path}/legacy.db"
    e = sa.create_engine(url)
    with e.begin() as c:
        c.execute(
            sa.text(
                "CREATE TABLE workflow_runs (run_id VARCHAR PRIMARY KEY, "
                "product_id VARCHAR, signal_id VARCHAR, status VARCHAR, "
                "current_stage VARCHAR, mode VARCHAR, recommendation_json TEXT, "
                "routing VARCHAR, composite_score FLOAT, created_at DATETIME, "
                "updated_at DATETIME, completed_at DATETIME, batch_id VARCHAR, "
                "prompt_tokens_total INTEGER, completion_tokens_total INTEGER)"
            )
        )
        rows = [
            ("r1", "p1", "sigA", "failed", "2026-01-01 00:00:00"),
            ("r2", "p1", "sigA", "failed", "2026-01-01 01:00:00"),
            ("r3", "p1", "sigA", "completed", "2026-01-01 02:00:00"),
            ("r4", "p2", "sigA", "completed", "2026-01-01 03:00:00"),
        ]
        for run_id, pid, sid, status, created in rows:
            c.execute(
                sa.text(
                    "INSERT INTO workflow_runs (run_id, product_id, signal_id, "
                    "status, created_at) VALUES (:r, :p, :s, :st, :c)"
                ),
                {"r": run_id, "p": pid, "s": sid, "st": status, "c": created},
            )
    e.dispose()

    store = SQLiteStore(url)
    assert (store.get_run("r1")["attempt_no"], store.get_run("r1")["root_run_id"]) == (1, None)
    assert (store.get_run("r2")["attempt_no"], store.get_run("r2")["root_run_id"]) == (2, "r1")
    assert (store.get_run("r3")["attempt_no"], store.get_run("r3")["root_run_id"]) == (3, "r1")
    # Different product → separate lineage, untouched.
    assert (store.get_run("r4")["attempt_no"], store.get_run("r4")["root_run_id"]) == (1, None)

    # A new retry continues the backfilled lineage.
    r5 = store.create_run(product_id="p1", signal_id="sigA")
    assert (store.get_run(r5)["attempt_no"], store.get_run(r5)["root_run_id"]) == (4, "r1")
