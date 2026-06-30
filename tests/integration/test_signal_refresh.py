"""Integration tests for POST /signals/{id}/refresh — re-ingest a signal's
content and re-run it on a fresh attempt lineage.

Signals are immutable after Gate 0 intake; refresh is the single audited path
that overwrites raw_content, for when the original crawl captured site-chrome /
a bot-wall page and a better fetch recovered the article (run d014f313 motivated
this). The new run is tagged origin='refresh' so the observatory shows a
re-ingest, not "attempt N of N" of a failure-retry lineage.

The background pipeline (_execute_s1_s2) is stubbed out — these tests cover the
refresh orchestration (void in-flight, overwrite content, re-infer category,
spawn a refresh-origin run), not the downstream stage execution.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import app.api.runs as runs_module
from app.api.deps import get_engine
from app.api.main import app
from app.factory import PMEngine
from app.services.context_loader import ContextLoader
from app.services.notifier import FanoutNotifier
from app.services.template_service import TemplateService
from app.storage.sqlite_store import SQLiteStore


@pytest.fixture()
def engine(tmp_path):
    store = SQLiteStore(f"sqlite:///{tmp_path}/test.db")
    return PMEngine(
        store=store,
        llm=AsyncMock(),
        context_loader=MagicMock(spec=ContextLoader),
        template_service=MagicMock(spec=TemplateService),
        notifier=MagicMock(spec=FanoutNotifier),
    )


@pytest.fixture()
def client(engine, monkeypatch):
    # Stub the background pipeline so refresh returns without running real stages.
    monkeypatch.setattr(runs_module, "_execute_s1_s2", AsyncMock())
    app.dependency_overrides[get_engine] = lambda: engine
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


def _seed_signal_with_run(
    engine: PMEngine, *, status: str = "waiting_direction"
) -> tuple[str, str]:
    signal_id = engine.store.save_signal(
        original_product_id="example-mobile-product",
        title="Rokarolla Android Malware Steals PINs",
        raw_content="thin headline-only capture",
        category="other",
    )
    run_id = engine.store.create_run("example-mobile-product", signal_id)
    engine.store.update_run(run_id, status=status, current_stage="s2")
    engine.store.update_signal_status(signal_id, "in_run")
    return signal_id, run_id


# Avoids any regulation-keyword substring (e.g. "disa" inside "disables") so the
# assertion holds regardless of whether the word-boundary category fix is present
# in the branch under test — the point here is that category is RE-INFERRED from
# the new body (Android → platform), not that it tests the matcher itself.
_FULL_BODY = (
    "A new Android banking trojan, Rokarolla, targets 217 banking apps, steals "
    "lock-screen PINs and SMS codes, and rewrites the clipboard."
)


def test_refresh_voids_inflight_updates_content_and_starts_refresh_run(client, engine):
    signal_id, old_run = _seed_signal_with_run(engine)

    resp = client.post(
        f"/signals/{signal_id}/refresh",
        json={"raw_content": _FULL_BODY, "product_id": "example-mobile-product",
              "note": "curl_cffi refetch"},
    )
    assert resp.status_code == 202
    body = resp.json()

    # Old in-flight run is voided (killed).
    assert old_run in body["voided_runs"]
    assert engine.store.get_run(old_run)["status"] == "killed"

    # Content overwritten, category re-inferred (Android → platform, not 'other'),
    # refreshed_at stamped.
    sig = engine.store.get_signal(signal_id)
    assert sig["raw_content"] == _FULL_BODY
    assert sig["category"] == "platform"
    assert body["category"] == "platform"
    assert sig["refreshed_at"] is not None

    # A new run was started, tagged origin='refresh', on a FRESH lineage (attempt 1).
    new_run_id = body["started"]["run_id"]
    assert new_run_id != old_run
    new_run = engine.store.get_run(new_run_id)
    assert new_run["origin"] == "refresh"
    assert new_run["attempt_no"] == 1
    assert new_run["root_run_id"] is None


def test_refresh_records_void_event_on_old_run(client, engine):
    signal_id, old_run = _seed_signal_with_run(engine)
    client.post(
        f"/signals/{signal_id}/refresh",
        json={"raw_content": _FULL_BODY, "product_id": "example-mobile-product"},
    )
    events = engine.store.get_approval_events(old_run)
    assert [e["action"] for e in events] == ["void"]
    assert "refresh" in events[0]["feedback_text"]


def test_refresh_empty_content_rejected(client, engine):
    signal_id, _ = _seed_signal_with_run(engine)
    resp = client.post(f"/signals/{signal_id}/refresh", json={"raw_content": "   "})
    assert resp.status_code == 422


def test_refresh_unknown_signal_404(client):
    resp = client.post("/signals/does-not-exist/refresh", json={"raw_content": "x"})
    assert resp.status_code == 404


def test_refresh_with_no_inflight_runs_still_starts_a_run(client, engine):
    """Refreshing a signal whose runs are already terminal voids nothing but
    still re-ingests and starts a fresh run."""
    signal_id, old_run = _seed_signal_with_run(engine, status="completed")
    resp = client.post(
        f"/signals/{signal_id}/refresh",
        json={"raw_content": _FULL_BODY, "product_id": "example-mobile-product"},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["voided_runs"] == []
    assert engine.store.get_run(old_run)["status"] == "completed"  # untouched
    assert engine.store.get_run(body["started"]["run_id"])["origin"] == "refresh"
