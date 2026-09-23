"""S2K bridge factory tests; full Hermes-agent CLI inference is retired."""

from __future__ import annotations

import shlex
import sys
from types import SimpleNamespace

from app.factory import build_s2k_llm_provider
from app.llm.s2k_bridge import S2KBridgeProvider


def test_s2k_factory_builds_the_configured_bounded_bridge(monkeypatch, tmp_path):
    import config

    profile = tmp_path / "s2k-profile"
    profile.mkdir()
    (profile / "config.yaml").write_text("auxiliary:\n  s2k: {}\n", encoding="utf-8")
    script = tmp_path / "s2k_completion.py"
    script.write_text("print('{}')\n", encoding="utf-8")
    script.chmod(0o700)
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    monkeypatch.setattr(
        config,
        "settings",
        SimpleNamespace(
            S2K_COMPLETION_COMMAND=command,
            S2K_COMPLETION_PROFILE_HOME=str(profile),
            S2K_COMPLETION_TIMEOUT_SECONDS=5,
            S2K_COMPLETION_MAX_STDOUT_BYTES=8192,
        ),
    )

    provider = build_s2k_llm_provider()

    assert isinstance(provider, S2KBridgeProvider)


def test_s2k_factory_fails_closed_without_an_isolated_profile(monkeypatch):
    import pytest

    import config

    monkeypatch.setattr(
        config,
        "settings",
        SimpleNamespace(
            S2K_COMPLETION_COMMAND=f"{sys.executable} /static/s2k_completion.py",
            S2K_COMPLETION_PROFILE_HOME="",
            S2K_COMPLETION_TIMEOUT_SECONDS=5,
            S2K_COMPLETION_MAX_STDOUT_BYTES=8192,
        ),
    )

    with pytest.raises(ValueError):
        build_s2k_llm_provider()
