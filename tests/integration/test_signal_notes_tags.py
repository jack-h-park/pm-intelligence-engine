"""Integration tests for signal review notes and tags.

Notes are append-only (no edit/delete path exists at all — that is the point);
tags are a mutable set. Both are exercised through the API so the store, the
serializers and the response models stay in agreement.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_engine
from app.api.main import app
from app.factory import PMEngine
from app.services.context_loader import ContextLoader
from app.services.notifier import FanoutNotifier
from app.services.signal_tags import MAX_TAGS_PER_SIGNAL
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
def client(engine):
    app.dependency_overrides[get_engine] = lambda: engine
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def signal_id(engine):
    return engine.store.save_signal(title="Some signal", raw_content="body text")


# --- Notes -----------------------------------------------------------------


def test_add_and_list_note(client, signal_id):
    resp = client.post(
        f"/signals/{signal_id}/notes",
        json={"body": "Relevant, but the regulatory angle is weak.",
              "author": "jack", "context": "gate1"},
    )
    assert resp.status_code == 201
    note = resp.json()
    assert note["author"] == "jack"
    assert note["context"] == "gate1"
    assert note["superseded_by"] is None

    listed = client.get(f"/signals/{signal_id}/notes").json()
    assert [n["note_id"] for n in listed] == [note["note_id"]]


def test_notes_accumulate_oldest_first(client, signal_id):
    for body in ("first", "second", "third"):
        client.post(
            f"/signals/{signal_id}/notes",
            json={"body": body, "author": "ops"},
        )
    listed = client.get(f"/signals/{signal_id}/notes").json()
    # Order is the order judgments were made — the whole value of the trail.
    assert [n["body"] for n in listed] == ["first", "second", "third"]


def test_no_edit_or_delete_route_exists(client, signal_id):
    """Append-only is enforced by the absence of a mutation path, so assert the
    absence — a future PATCH/DELETE would silently break the guarantee."""
    resp = client.post(
        f"/signals/{signal_id}/notes", json={"body": "n", "author": "jack"}
    )
    note_id = resp.json()["note_id"]
    # 404 (no such route) or 405 (route exists, verb not allowed) both mean "no
    # mutation path" — assert on either so the test tracks the guarantee rather
    # than the routing detail that produces it.
    assert client.patch(f"/signals/{signal_id}/notes/{note_id}").status_code in (404, 405)
    assert client.delete(f"/signals/{signal_id}/notes/{note_id}").status_code in (404, 405)


def test_agent_and_human_notes_stay_distinguishable(client, signal_id):
    """The hazard this guards: cron agents write here far more often than Jack
    does, and a mutable note would let a sweep clobber a hand-written one."""
    client.post(f"/signals/{signal_id}/notes",
                json={"body": "started per grounded triage", "author": "ops"})
    client.post(f"/signals/{signal_id}/notes",
                json={"body": "disagree — worth a deeper look", "author": "jack"})
    notes = client.get(f"/signals/{signal_id}/notes").json()
    assert [n["author"] for n in notes] == ["ops", "jack"]
    assert len(notes) == 2  # the agent note did not displace the human one


@pytest.mark.parametrize(
    "payload",
    [{"body": "   ", "author": "jack"}, {"body": "x", "author": "  "}],
)
def test_blank_body_or_author_rejected(client, signal_id, payload):
    resp = client.post(f"/signals/{signal_id}/notes", json=payload)
    assert resp.status_code == 422


def test_note_on_unknown_signal_404(client):
    resp = client.post(
        "/signals/does-not-exist/notes", json={"body": "x", "author": "jack"}
    )
    assert resp.status_code == 404


def test_list_notes_unknown_signal_404(client):
    # 404 rather than [] — a typo'd id must not read as "no notes yet".
    assert client.get("/signals/does-not-exist/notes").status_code == 404


# --- Tags ------------------------------------------------------------------


def test_add_tags_normalizes_and_dedupes(client, signal_id):
    resp = client.post(
        f"/signals/{signal_id}/tags",
        json={"tags": ["Regulation", "gate_1_blocked", "regulation"],
              "author": "jack"},
    )
    assert resp.status_code == 200
    assert resp.json()["tags"] == ["gate-1-blocked", "regulation"]


def test_adding_existing_tag_is_idempotent(client, signal_id):
    client.post(f"/signals/{signal_id}/tags",
                json={"tags": ["regulation"], "author": "jack"})
    resp = client.post(f"/signals/{signal_id}/tags",
                       json={"tags": ["Regulation"], "author": "ops"})
    assert resp.json()["tags"] == ["regulation"]

    # First author wins — the person who first applied the label made the call.
    notes = client.get(f"/signals/{signal_id}").json()
    assert notes["tags"] == ["regulation"]


def test_remove_tag(client, signal_id):
    client.post(f"/signals/{signal_id}/tags",
                json={"tags": ["a", "b"], "author": "jack"})
    resp = client.post(f"/signals/{signal_id}/tags/remove", json={"tags": ["A"]})
    assert resp.json()["tags"] == ["b"]


def test_removing_absent_tag_is_not_an_error(client, signal_id):
    resp = client.post(f"/signals/{signal_id}/tags/remove", json={"tags": ["ghost"]})
    assert resp.status_code == 200
    assert resp.json()["tags"] == []


def test_unnormalizable_tag_422(client, signal_id):
    resp = client.post(f"/signals/{signal_id}/tags",
                       json={"tags": ["!!!"], "author": "jack"})
    assert resp.status_code == 422


def test_tag_cap_enforced(client, signal_id):
    too_many = [f"tag-{i}" for i in range(MAX_TAGS_PER_SIGNAL + 1)]
    resp = client.post(f"/signals/{signal_id}/tags",
                       json={"tags": too_many, "author": "jack"})
    assert resp.status_code == 422


def test_tags_on_unknown_signal_404(client):
    resp = client.post("/signals/nope/tags", json={"tags": ["x"], "author": "j"})
    assert resp.status_code == 404


# --- Signal read surface ---------------------------------------------------


def test_signal_response_carries_tags_and_note_count(client, engine, signal_id):
    client.post(f"/signals/{signal_id}/tags",
                json={"tags": ["regulation"], "author": "jack"})
    client.post(f"/signals/{signal_id}/notes",
                json={"body": "worth watching", "author": "jack"})

    listed = client.get("/signals").json()
    assert listed[0]["tags"] == ["regulation"]
    assert listed[0]["note_count"] == 1
    # Note bodies stay off the list response, like raw_content.
    assert "notes" not in listed[0]


def test_signal_without_annotations_defaults_empty(client, signal_id):
    body = client.get(f"/signals/{signal_id}").json()
    assert body["tags"] == []
    assert body["note_count"] == 0


def test_list_signals_filters_by_tag(client, engine):
    tagged = engine.store.save_signal(title="tagged", raw_content="x")
    engine.store.save_signal(title="untagged", raw_content="x")
    client.post(f"/signals/{tagged}/tags",
                json={"tags": ["regulation"], "author": "jack"})

    # Filter accepts the tag as typed, not only pre-normalised.
    resp = client.get("/signals", params={"tag": "Regulation"})
    assert [s["signal_id"] for s in resp.json()] == [tagged]


def test_tag_filter_unnormalizable_422(client):
    assert client.get("/signals", params={"tag": "!!!"}).status_code == 422
