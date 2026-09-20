"""The review link must point at a surface a browser can actually open.

`BASE_URL`'s own docstring calls it "the base URL used to generate review page
links", and for a long time those links addressed this service's own
`/runs/{id}/review` page. That page is behind `require_auth`, which reads the
`Authorization` header and nothing else — so every link was a 401 in the one
place it gets clicked. `REVIEW_UI_BASE_URL` lets a deployment point at a
browser-reachable console instead.

These exercise the builder rather than asserting prose about it, because the
two shapes differ by a path segment and that is exactly the kind of difference
a second call site gets wrong.
"""

import importlib
import os

import pytest


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    """Reload `config` per test so Settings picks the patched environment up.

    Runs from an empty directory. ``Settings`` declares ``env_file=".env"``,
    which pydantic resolves against the *current working directory*, so
    clearing a variable from the process environment does not stop a real
    ``.env`` from supplying it. These tests passed on a machine whose checkout
    had no ``.env`` and failed on the host that runs the service, where the live
    one sets REVIEW_UI_BASE_URL — the test was reading deployment config instead
    of its own fixture.
    """

    original_cwd = os.getcwd()

    def _load(**env):
        monkeypatch.chdir(tmp_path)
        for key in ("BASE_URL", "REVIEW_UI_BASE_URL"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        import config

        return importlib.reload(config)

    yield _load

    # Undo the patches BEFORE the final reload, and restore the directory too.
    # That reload is what every later test in the session inherits as
    # `config.settings`, and pytest tears `monkeypatch` down *after* this
    # fixture — so reloading first would rebuild settings from this test's own
    # patched values (the last case sets both bases to "") and hand the empty
    # result to everything that runs next. Reproduced as `assert None == ''` in
    # an integration test two files away.
    monkeypatch.undo()
    os.chdir(original_cwd)
    import config

    importlib.reload(config)


RUN = "94f165f6-ad40-4a66-94ce-0ed5f363a970"


def test_defaults_to_the_built_in_review_page(cfg):
    c = cfg(BASE_URL="http://localhost:8000")
    assert c.review_url_for(RUN) == f"http://localhost:8000/runs/{RUN}/review"


def test_review_ui_wins_and_drops_the_review_segment(cfg):
    """The external console renders the run at /runs/<id> — no /review below it,
    so carrying the old path over would 404 on the very surface this exists for."""
    c = cfg(BASE_URL="http://localhost:8000", REVIEW_UI_BASE_URL="https://console.example")
    assert c.review_url_for(RUN) == f"https://console.example/runs/{RUN}"


def test_trailing_slash_does_not_double_up(cfg):
    c = cfg(REVIEW_UI_BASE_URL="https://console.example/")
    assert c.review_url_for(RUN) == f"https://console.example/runs/{RUN}"


def test_no_base_configured_yields_no_link(cfg):
    """Callers treat "" as 'emit no review_url'. A relative link would render as
    a link and go nowhere, which is worse than the field being absent."""
    c = cfg(BASE_URL="", REVIEW_UI_BASE_URL="")
    assert c.review_url_for(RUN) == ""
