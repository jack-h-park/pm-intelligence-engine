import sys
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_hermes_oauth_provider_returns_cli_json_without_api_key_usage(tmp_path):
    """The new insight provider must use the OAuth CLI, never an API-key SDK."""
    from app.llm.hermes_oauth import HermesOAuthProvider

    executable = tmp_path / "oauth_cli.py"
    executable.write_text(
        "import json, sys\n"
        "assert '--profile' in sys.argv\n"
        "assert 'ops' in sys.argv\n"
        "assert '-z' in sys.argv\n"
        "print(json.dumps({'headline': 'Verified insight'}))\n",
        encoding="utf-8",
    )
    provider = HermesOAuthProvider(command=(sys.executable, str(executable)), profile="ops")
    usage: list[dict[str, int]] = []

    result = await provider.complete(
        messages=[{"role": "user", "content": "Return JSON only."}], usage_sink=usage
    )

    assert result == '{"headline": "Verified insight"}'
    assert usage == [{"input_tokens": 0, "output_tokens": 0}]


def test_insight_provider_factory_selects_oauth_without_changing_legacy_provider(monkeypatch):
    import config
    from app.factory import build_insight_llm_provider
    from app.llm.hermes_oauth import HermesOAuthProvider

    monkeypatch.setattr(
        config,
        "settings",
        SimpleNamespace(INSIGHT_OAUTH_COMMAND=f"{sys.executable} -m hermes_cli.main", INSIGHT_OAUTH_PROFILE="ops"),
    )

    provider = build_insight_llm_provider()

    assert isinstance(provider, HermesOAuthProvider)
