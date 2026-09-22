"""Global test fixtures shared by unit and integration suites.

The run exporter writes to the repo-canonical archive (repo_root/archive/runs)
via ``_canonical_archive_root()``, which is derived from ``__file__`` — NOT
from ``config.settings``. Overriding DECISION_SYSTEM_ROOT therefore does not
stop the write: any test that drives a real export (directly, or through
``finalize_run`` on a completed note/structure/evaluate/decide run) would
pollute the working tree with untracked ``archive/runs/<product>/...`` dirs.

The autouse ``isolate_run_archive`` fixture points the archive root at the
test's ``tmp_path`` so the suite never writes into the repository. Tests that
need the redirected root (e.g. to assert on exported files) can request
``isolate_run_archive`` and use its return value.
"""

import pytest

_BASELINE_SETTINGS: dict[str, object] = {}

_SETTINGS_REPLACED = (
    "this test left config.settings bound to a different object than the rest "
    "of the session holds: every module that did `from config import settings` "
    "still has the original. Override attributes on the existing singleton "
    "instead of reloading the config module, and restore the attribute if you "
    "replace the object."
)


def pytest_sessionstart(session):
    """Record the configuration singleton before any test can replace it.

    Recorded here rather than on first use: the first test of the session is as
    able to leak as any other, and a baseline sampled after it has run would
    adopt the leak as the expected value.
    """
    import config

    _BASELINE_SETTINGS["object"] = config.settings


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    """Fail the test that leaves ``config.settings`` bound to a new object.

    Every module under ``app/`` binds the singleton at import time with
    ``from config import settings``. Rebinding the module attribute — which is
    what ``importlib.reload(config)`` does — leaves those modules holding the
    previous object, so the session carries two configurations: the one a later
    test patches through ``config.settings``, and the one the code under test
    actually reads. ``tests/unit/test_review_url.py`` reloaded the module and
    cost six insight-worker tests, which failed only when that file was
    collected first. The failure is silent and order-dependent, so it is
    asserted here rather than debugged a second time.

    This is a hook rather than an autouse fixture because it has to observe the
    state a test *leaves behind*: pytest orders the autouse fixtures of this
    file after ``monkeypatch``, so a fixture's teardown would run before
    ``monkeypatch.undo()`` and flag restored patches as leaks. The default
    ``pytest_runtest_teardown`` implementation is what finalizes fixtures, so
    checking after it yields the post-restore state.

    Replacing the attribute *within* a test is therefore fine as long as it is
    restored (``monkeypatch.setattr(config, "settings", ...)``); so is mutating
    fields on the singleton (``monkeypatch.setattr(settings, "FIELD", ...)``),
    which is the supported way to override configuration.
    """
    import config

    expected = _BASELINE_SETTINGS["object"]
    result = yield
    assert config.settings is expected, f"{item.nodeid}: {_SETTINGS_REPLACED}"
    return result


@pytest.fixture(autouse=True)
def isolate_run_archive(tmp_path, monkeypatch):
    from app.services import run_exporter

    archive_root = tmp_path / "archive" / "runs"
    monkeypatch.setattr(run_exporter, "_canonical_archive_root", lambda: archive_root)
    return archive_root


@pytest.fixture(autouse=True)
def supply_test_fallback_key(monkeypatch):
    """Supply inert credentials for application startup during local tests."""
    from config import settings

    if not settings.OPENAI_API_KEY:
        monkeypatch.setattr(settings, "OPENAI_API_KEY", "test-openai-key")
    if not settings.ANTHROPIC_API_KEY:
        monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "test-anthropic-key")
