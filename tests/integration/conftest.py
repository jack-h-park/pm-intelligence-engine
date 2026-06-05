"""Shared integration-test fixtures.

Server-side auth (``require_auth``) is now applied to every router. The
business-logic integration tests are not concerned with auth, so this autouse
fixture bypasses it by default. Tests that exercise auth itself
(``test_auth.py``) remove this override and assert real enforcement.
"""

import pytest

from app.api.deps import require_auth
from app.api.main import app


@pytest.fixture(autouse=True)
def bypass_auth():
    app.dependency_overrides[require_auth] = lambda: None
    yield
    app.dependency_overrides.pop(require_auth, None)
