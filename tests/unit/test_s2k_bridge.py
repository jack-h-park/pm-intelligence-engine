from __future__ import annotations

import json
import shlex
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.llm.s2k_bridge import S2KBridgeError, S2KBridgeProvider, build_s2k_llm_provider


def _profile(tmp_path: Path) -> Path:
    profile = tmp_path / "s2k-profile"
    profile.mkdir()
    (profile / "config.yaml").write_text("auxiliary:\n  s2k: {}\n", encoding="utf-8")
    return profile


def _executable(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "fixture_bridge.py"
    path.write_text(body, encoding="utf-8")
    path.chmod(0o700)
    return path


_UNSET = object()


def _success(route: dict | None = None, usage=_UNSET) -> dict:
    return {
        "text": "fixture answer",
        "route": route or {"provider": "anthropic", "model": "fixture-model"},
        "usage": {"input_tokens": 6, "output_tokens": 4} if usage is _UNSET else usage,
    }


@pytest.mark.asyncio
async def test_bridge_sends_prompt_on_stdin_and_filters_child_environment(tmp_path, monkeypatch):
    profile = _profile(tmp_path)
    audit = tmp_path / "child-audit.json"
    script = _executable(
        tmp_path,
        f"""import json, os, sys
from pathlib import Path
request = json.load(sys.stdin)
audit = {{"argv": sys.argv, "env": dict(os.environ), "request": request}}
Path({str(audit)!r}).write_text(json.dumps(audit))
print(json.dumps({_success()!r}))
""",
    )
    provider = S2KBridgeProvider((sys.executable, str(script)), str(profile), 2, 4096)
    prompt = "PRIVATE_PROMPT_MUST_NOT_BE_IN_ARGV"
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_BASE_URL", "LLM_PROVIDER"):
        monkeypatch.setenv(key, "fixture-secret-or-provider")
    usage = []
    events = []
    monkeypatch.setattr("app.llm.s2k_bridge.emit_event", lambda *args: events.append(args))

    result = await provider.complete([{"role": "user", "content": prompt}], usage_sink=usage)

    observed = json.loads(audit.read_text(encoding="utf-8"))
    assert result == "fixture answer"
    assert observed["request"]["messages"][0]["content"] == prompt
    assert prompt not in " ".join(observed["argv"])
    assert not {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_BASE_URL", "LLM_PROVIDER"} & set(
        observed["env"]
    )
    assert observed["env"]["HERMES_HOME"] == str(profile)
    assert set(observed["env"]) <= {
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "TEMP",
        "TMP",
        "HERMES_HOME",
        "__CF_USER_TEXT_ENCODING",
    }
    assert usage == [
        {
            "input_tokens": 6,
            "output_tokens": 4,
            "model": "fixture-model",
            "provider": "anthropic",
            "tokens_available": True,
        }
    ]
    assert events[0][0:2] == ("s2k_inference", "route_selected")
    uuid.UUID(events[0][2])
    assert events[0][3] == {
        "provider": "anthropic",
        "model": "fixture-model",
        "usage_status": "measured",
    }
    assert prompt not in json.dumps(events)


@pytest.mark.asyncio
async def test_unknown_usage_does_not_append_fabricated_zeroes(tmp_path):
    profile = _profile(tmp_path)
    script = _executable(tmp_path, f"import json\nprint(json.dumps({_success(usage=None)!r}))\n")
    provider = S2KBridgeProvider((sys.executable, str(script)), str(profile), 2, 4096)
    usage = []

    assert (
        await provider.complete([{"role": "user", "content": "fixture"}], usage_sink=usage)
        == "fixture answer"
    )
    assert usage == []


@pytest.mark.parametrize(
    "response",
    [
        {"text": "answer", "usage": None},
        {"text": "answer", "route": {"provider": "openrouter", "model": "bad"}, "usage": None},
        _success(usage={"input_tokens": 0, "output_tokens": 0}),
        _success(usage={"input_tokens": -1, "output_tokens": 2}),
    ],
    ids=("absent-route", "unapproved-route", "fabricated-zero-usage", "negative-usage"),
)
@pytest.mark.asyncio
async def test_bridge_rejects_invalid_route_or_usage(tmp_path, response):
    profile = _profile(tmp_path)
    script = _executable(tmp_path, f"import json\nprint(json.dumps({response!r}))\n")
    provider = S2KBridgeProvider((sys.executable, str(script)), str(profile), 2, 4096)

    with pytest.raises(S2KBridgeError):
        await provider.complete([{"role": "user", "content": "fixture"}])


@pytest.mark.parametrize(
    "body",
    [
        "print('not-json')\n",
        "import sys\nsys.stderr.write('PRIVATE_ERROR_OUTPUT')\nsys.exit(7)\n",
        "print('x' * 200000)\n",
        "import time\ntime.sleep(10)\n",
    ],
    ids=("malformed-json", "nonzero-exit", "oversized-stdout", "timeout"),
)
@pytest.mark.asyncio
async def test_bridge_process_failures_are_bounded_and_sanitized(tmp_path, body):
    profile = _profile(tmp_path)
    script = _executable(tmp_path, body)
    provider = S2KBridgeProvider((sys.executable, str(script)), str(profile), 0.15, 4096)

    with pytest.raises(S2KBridgeError) as exc_info:
        await provider.complete([{"role": "user", "content": "PRIVATE_MODEL_OUTPUT"}])

    assert "PRIVATE" not in str(exc_info.value)
    assert "not-json" not in str(exc_info.value)
    assert "x" * 100 not in str(exc_info.value)


@pytest.mark.asyncio
async def test_bridge_rejects_oversized_stderr_and_reaps_child(tmp_path):
    profile = _profile(tmp_path)
    script = _executable(tmp_path, "import sys\nsys.stderr.write('x' * 70000)\nprint('{}')\n")
    provider = S2KBridgeProvider((sys.executable, str(script)), str(profile), 2, 4096)

    with pytest.raises(S2KBridgeError) as exc_info:
        await provider.complete([{"role": "user", "content": "fixture"}])

    assert "x" * 100 not in str(exc_info.value)


@pytest.mark.asyncio
async def test_oversized_stdout_while_child_does_not_read_large_stdin_fails_promptly(tmp_path):
    profile = _profile(tmp_path)
    script = _executable(
        tmp_path,
        "import os\nos.write(1, b'x' * 10485760)\n",
    )
    provider = S2KBridgeProvider((sys.executable, str(script)), str(profile), 2, 4096)
    started = __import__("time").monotonic()

    with pytest.raises(S2KBridgeError) as exc_info:
        await provider.complete([{"role": "user", "content": "x" * 1_048_576}])

    elapsed = __import__("time").monotonic() - started
    assert exc_info.value.kind == "stdout_too_large"
    assert elapsed < 2


@pytest.mark.parametrize("missing", ("command", "profile"))
def test_factory_fails_closed_when_command_or_profile_is_missing(monkeypatch, missing, tmp_path):
    import config

    profile = _profile(tmp_path)
    script = _executable(tmp_path, "print('{}')\n")
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    monkeypatch.setattr(
        config,
        "settings",
        SimpleNamespace(
            S2K_COMPLETION_COMMAND="" if missing == "command" else command,
            S2K_COMPLETION_PROFILE_HOME="" if missing == "profile" else str(profile),
            S2K_COMPLETION_TIMEOUT_SECONDS=30,
            S2K_COMPLETION_MAX_STDOUT_BYTES=1_048_576,
        ),
    )

    with pytest.raises(ValueError):
        build_s2k_llm_provider()


def test_factory_builds_only_the_configured_bridge_command(monkeypatch, tmp_path):
    import config

    profile = _profile(tmp_path)
    script = _executable(tmp_path, "print('{}')\n")
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    monkeypatch.setattr(
        config,
        "settings",
        SimpleNamespace(
            S2K_COMPLETION_COMMAND=command,
            S2K_COMPLETION_PROFILE_HOME=str(profile),
            S2K_COMPLETION_TIMEOUT_SECONDS=2,
            S2K_COMPLETION_MAX_STDOUT_BYTES=4096,
        ),
    )

    provider = build_s2k_llm_provider()

    assert isinstance(provider, S2KBridgeProvider)


@pytest.mark.asyncio
async def test_bridge_does_not_allow_callers_to_override_profile_model(tmp_path):
    profile = _profile(tmp_path)
    marker = tmp_path / "process-started"
    script = _executable(
        tmp_path, f"from pathlib import Path\nPath({str(marker)!r}).touch()\nprint('{{}}')\n"
    )
    provider = S2KBridgeProvider((sys.executable, str(script)), str(profile), 2, 4096)

    with pytest.raises(ValueError, match="selected by the isolated profile"):
        await provider.complete([{"role": "user", "content": "fixture"}], model="fixture-model")

    assert not marker.exists()


def test_product_decision_openai_factory_remains_independent(monkeypatch):
    import app.llm.claude as claude_module
    import app.llm.openai as openai_module
    import config
    from app.factory import _build_raw_llm_provider
    from app.llm.tiered_fallback import TieredFallbackProvider

    class FakeProvider:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(
        config,
        "settings",
        SimpleNamespace(
            LLM_PROVIDER="openai",
            OPENAI_API_KEY="fixture-openai-key",
            OPENAI_MODEL="fixture-openai-model",
            ANTHROPIC_API_KEY="fixture-anthropic-key",
            ANTHROPIC_MODEL="fixture-anthropic-model",
        ),
    )
    monkeypatch.setattr(openai_module, "OpenAIProvider", FakeProvider)
    monkeypatch.setattr(claude_module, "ClaudeProvider", FakeProvider)

    provider = _build_raw_llm_provider()

    assert isinstance(provider, TieredFallbackProvider)
    assert isinstance(provider._primary, FakeProvider)
    assert provider._primary.kwargs == {
        "api_key": "fixture-openai-key",
        "default_model": "fixture-openai-model",
    }
