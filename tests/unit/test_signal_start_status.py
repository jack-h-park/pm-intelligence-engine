from datetime import UTC, datetime

import pytest
from sqlalchemy import event, text

from app.services.signal_start_status import classify_start_status
from app.storage.sqlite_store import SQLiteStore

NOW = datetime(2026, 11, 11, 1, tzinfo=UTC)


def row(**kw):
    return dict(
        signal_id="00000000-0000-0000-0000-000000000001",
        title="Decision",
        product_id=None,
        status="new",
        ingested_at="2026-11-10T00:00:00Z",
        run_count=0,
        failed_count=0,
        last_attempt_at=None,
        **kw,
    )


@pytest.mark.parametrize(
    "status,count,failed,kind",
    [
        ("new", 0, 0, "stranded"),
        ("in_run", 0, 0, "stranded"),
        ("done", 0, 0, None),
        ("blocked", 0, 0, None),
        ("new", 2, 2, "abandoned"),
        ("new", 2, 1, None),
        ("in_run", 2, 2, None),
        ("new", 1, 0, None),
        ("done", 1, 1, None),
    ],
)
def test_legacy_domain_predicates(status, count, failed, kind):
    item = row()
    item.update(
        status=status, run_count=count, failed_count=failed, last_attempt_at="2026-11-10T00:00:00Z"
    )
    result = classify_start_status([item], now=NOW)
    assert result["complete"] is True and result["schema_version"] == 1
    assert [i["classification"] for i in result["items"]] == ([kind] if kind else [])


@pytest.mark.parametrize(
    "kind,anchor,expected",
    [
        ("stranded", "2026-11-11T00:00:01Z", False),
        ("stranded", "2026-11-11T00:00:00Z", True),
        ("stranded", "2026-11-11T02:00:00Z", False),
        ("abandoned", "2026-11-10T19:00:01Z", False),
        ("abandoned", "2026-11-10T19:00:00Z", True),
        ("abandoned", "2026-11-10T20:00:00+01:00", True),
    ],
)
def test_grace_boundary_and_original_age_anchors(kind, anchor, expected):
    item = row()
    item.update(
        ingested_at=anchor if kind == "stranded" else "2020-01-01",
        last_attempt_at=anchor,
        run_count=0 if kind == "stranded" else 2,
        failed_count=0 if kind == "stranded" else 2,
    )
    result = classify_start_status([item], now=NOW)
    assert result["items"][0]["eligible"] is expected
    assert result["counts"][kind] == 1
    assert result["counts"]["eligible"] == int(expected)


@pytest.mark.parametrize("anchor", [None, "invalid", ""])
def test_unknown_age_preserves_legacy_eligibility(anchor):
    item = row()
    item["ingested_at"] = anchor
    result = classify_start_status([item], now=NOW)
    assert result["items"][0]["eligible"] and result["items"][0]["age_unknown"]


def test_targeted_nonfresh_signal_and_policy_override():
    item = row()
    item.update(status="done", run_count=1)
    result = classify_start_status([item], now=NOW, targeted=True, grace_hours=0)
    assert result["items"][0]["classification"] == "none" and not result["items"][0]["fresh"]
    assert result["policy"] == {"grace_hours": 0, "failed_grace_hours": 6}


@pytest.mark.parametrize("grace", [float("nan"), float("inf"), -1, 721])
def test_invalid_policy_is_rejected(grace):
    with pytest.raises(ValueError):
        classify_start_status([], now=NOW, grace_hours=grace)


def test_store_snapshot_is_one_read_statement_and_covers_more_than_one_page(tmp_path):
    store = SQLiteStore(f"sqlite:///{tmp_path}/recovery.db")
    ids = [
        store.save_signal(original_product_id=None, title=f"Signal {i}", raw_content="Body")
        for i in range(205)
    ]
    run = store.create_run("example", ids[0])
    with store._engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE workflow_runs SET lifecycle='done', outcome='failed', "
                "created_at='2026-11-10T00:00:00' WHERE run_id=:id"
            ),
            {"id": run},
        )
        conn.execute(text("UPDATE signals SET status='new', ingested_at='2026-11-10T00:00:00'"))
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(store._engine, "before_cursor_execute", capture)
    try:
        rows = store.signal_start_snapshot()
    finally:
        event.remove(store._engine, "before_cursor_execute", capture)
    assert len(statements) == 1 and statements[0].lstrip().upper().startswith("SELECT")
    result = classify_start_status(rows, now=NOW)
    assert result["counts"] == {"stranded": 204, "abandoned": 1, "eligible": 205}
    assert len({i["signal_id"] for i in result["items"]}) == 205
    # Snapshot classification and targeted lookup do not require store recreation.
    store.create_run("example", ids[0])
    after = classify_start_status(store.signal_start_snapshot(), now=NOW)
    assert after["counts"]["abandoned"] == 0
    assert len(store.signal_start_snapshot(ids[0])) == 1
