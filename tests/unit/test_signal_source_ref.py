"""signals.source_ref — provenance back-link to the originating sensing file.

Gate 0 submit passes the sensing filename so the observatory can pair a sensing
file with its run deterministically instead of fuzzy-matching titles. The column
is nullable: signals not submitted through Gate 0 (manual POST, replays) have no
provenance and read back as None.
"""
import sqlalchemy as sa
from app.storage.sqlite_store import SQLiteStore


def _store(tmp_path):
    return SQLiteStore(f"sqlite:///{tmp_path}/t.db")


def test_source_ref_round_trips(tmp_path):
    s = _store(tmp_path)
    fname = "2026-05-28-anthropic-glasswing-initial-update.md"
    sid = s.save_signal(title="Project Glasswing", raw_content="r", source_ref=fname)
    assert s.get_signal(sid)["source_ref"] == fname


def test_source_ref_defaults_to_none(tmp_path):
    s = _store(tmp_path)
    sid = s.save_signal(title="manual signal", raw_content="r")
    assert s.get_signal(sid)["source_ref"] is None


def test_migration_adds_source_ref_to_legacy_db(tmp_path):
    """A pre-existing DB whose signals table predates source_ref gains the column
    on next open, existing rows back-fill to NULL, and re-opening is a no-op."""
    url = f"sqlite:///{tmp_path}/legacy.db"
    e = sa.create_engine(url)
    with e.begin() as c:
        c.execute(
            sa.text(
                "CREATE TABLE signals (signal_id VARCHAR PRIMARY KEY, "
                "original_product_id VARCHAR, title VARCHAR, source_url VARCHAR, "
                "raw_content TEXT, category VARCHAR, status VARCHAR, "
                "source_type VARCHAR, ingested_at DATETIME)"
            )
        )
        c.execute(
            sa.text(
                "INSERT INTO signals VALUES ('s1', NULL, 'old', NULL, 'r', "
                "'other', 'new', 'manual', '2026-01-01 00:00:00')"
            )
        )
    e.dispose()

    store = SQLiteStore(url)
    assert store.get_signal("s1")["source_ref"] is None  # back-filled NULL
    sid = store.save_signal(title="new", raw_content="r", source_ref="file.md")
    assert store.get_signal(sid)["source_ref"] == "file.md"

    # Idempotent: a second open over the migrated DB must not raise.
    reopened = SQLiteStore(url)
    assert reopened.get_signal("s1")["source_ref"] is None
