"""Global test fixtures shared by unit and integration suites.

The run exporter writes to the repo-canonical archive (repo_root/archive/runs)
via ``_canonical_archive_root()``, which is derived from ``__file__`` — NOT
from ``config.settings``. Overriding DECISION_SYSTEM_ROOT therefore does not
stop the write: any test that drives a real export (directly, or through
``finalize_run`` on a completed note/structure/evaluate/decide run) would
pollute the working tree with untracked ``archive/runs/<product>/...`` dirs.

This autouse fixture points the archive root at the test's ``tmp_path`` so the
suite never writes into the repository. Tests that need the redirected root
(e.g. to assert on exported files) can request ``isolate_run_archive`` and use
its return value.
"""

import pytest


@pytest.fixture(autouse=True)
def isolate_run_archive(tmp_path, monkeypatch):
    from app.services import run_exporter

    archive_root = tmp_path / "archive" / "runs"
    monkeypatch.setattr(
        run_exporter, "_canonical_archive_root", lambda: archive_root
    )
    return archive_root
