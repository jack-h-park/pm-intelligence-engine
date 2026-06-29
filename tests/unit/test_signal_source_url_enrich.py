"""POST /signals backfills source_url from the sensing file frontmatter.

The Gate 0 submit body is composed by an LLM and occasionally omits source_url
(and signals ingested before that instruction landed have none). The URL is
always on disk in the sensing file's `url:` frontmatter, so when the caller gives
a deterministic source_ref but no source_url, the engine recovers it. An explicit
source_url always wins; every failure mode (no ref, missing file, no frontmatter
url, traversal-looking ref) degrades to None without failing the submit.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient

from app.api.deps import get_engine, require_auth
from app.api.main import app
from app.api.signals import _sensing_source_url
from app.factory import PMEngine
from app.services.context_loader import ContextLoader
from app.services.notifier import FanoutNotifier
from app.services.template_service import TemplateService
from app.storage.sqlite_store import SQLiteStore


# ─── Fixtures ──────────────────────────────────────────────────────────────────

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
    app.dependency_overrides[require_auth] = lambda: None
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def wiki_root(tmp_path, monkeypatch):
    """A WIKI_ROOT with the sensing subtree; GATE0_STATE_FILE left disabled."""
    sensing = tmp_path / "raw" / "from-web" / "sensing"
    sensing.mkdir(parents=True)
    monkeypatch.setattr("config.settings.WIKI_ROOT", str(tmp_path))
    monkeypatch.setattr("config.settings.GATE0_STATE_FILE", "")
    return sensing


def _write_sensing(sensing, fname, url="https://thehackernews.com/2026/06/x.html"):
    body = f"---\nsource: rss\nurl: {url}\ndate_sensed: 2026-06-18\n---\n\nBody text.\n"
    (sensing / fname).write_text(body, encoding="utf-8")
    return fname


# ─── _sensing_source_url unit tests ──────────────────────────────────────────

def test_reads_url_from_frontmatter(wiki_root):
    fname = _write_sensing(wiki_root, "2026-06-18-thehackernews-rokarolla.md")
    assert _sensing_source_url(fname) == "https://thehackernews.com/2026/06/x.html"


def test_quoted_url_is_unwrapped(wiki_root):
    _write_sensing(wiki_root, "q.md", url='"https://example.com/a"')
    assert _sensing_source_url("q.md") == "https://example.com/a"


def test_missing_file_returns_none(wiki_root):
    assert _sensing_source_url("does-not-exist.md") is None


def test_no_url_in_frontmatter_returns_none(wiki_root):
    (wiki_root / "n.md").write_text("---\nsource: rss\n---\n\nBody.\n", encoding="utf-8")
    assert _sensing_source_url("n.md") is None


def test_null_url_placeholder_returns_none(wiki_root):
    _write_sensing(wiki_root, "p.md", url="null")
    assert _sensing_source_url("p.md") is None


def test_traversal_ref_returns_none(wiki_root):
    assert _sensing_source_url("../../../etc/passwd") is None
    assert _sensing_source_url("") is None


def test_url_only_matched_inside_frontmatter(wiki_root):
    # A `url:` line in the body (after the closing ---) must not be picked up.
    (wiki_root / "b.md").write_text(
        "---\nsource: rss\n---\n\nurl: https://body-not-frontmatter.example\n",
        encoding="utf-8",
    )
    assert _sensing_source_url("b.md") is None


# ─── POST /signals integration ───────────────────────────────────────────────

def test_post_backfills_source_url_when_omitted(client, engine, wiki_root):
    fname = _write_sensing(wiki_root, "2026-06-18-rokarolla.md")
    resp = client.post("/signals", json={
        "title": "RokaRolla Android malware",
        "raw_content": "content",
        "source_ref": fname,
        "source_type": "web",
    })
    assert resp.status_code == 201
    assert resp.json()["source_url"] == "https://thehackernews.com/2026/06/x.html"


def test_post_explicit_source_url_wins(client, wiki_root):
    fname = _write_sensing(wiki_root, "f.md", url="https://from-frontmatter.example")
    resp = client.post("/signals", json={
        "title": "T",
        "raw_content": "content",
        "source_url": "https://explicit.example/article",
        "source_ref": fname,
        "source_type": "web",
    })
    assert resp.status_code == 201
    assert resp.json()["source_url"] == "https://explicit.example/article"


def test_post_no_source_ref_leaves_source_url_none(client, wiki_root):
    resp = client.post("/signals", json={"title": "Manual", "raw_content": "content"})
    assert resp.status_code == 201
    assert resp.json()["source_url"] is None


def test_post_missing_sensing_file_degrades_to_none(client, wiki_root):
    resp = client.post("/signals", json={
        "title": "T",
        "raw_content": "content",
        "source_ref": "2026-06-18-not-on-disk.md",
        "source_type": "web",
    })
    assert resp.status_code == 201
    assert resp.json()["source_url"] is None
