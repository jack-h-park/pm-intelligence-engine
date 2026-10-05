from __future__ import annotations

import asyncio
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


# The real bridge child reads its request before answering. A fixture child that
# answers without reading races the parent's stdin write: when the child has exited
# before the write lands, the write fails and the bridge reports
# bridge_process_failed. Any test that expects a successful completion, or a specific
# failure kind, reads first.
_READ_REQUEST = "import sys\nsys.stdin.read()\n"


_UNSET = object()


def _success(route: dict | None = None, usage=_UNSET) -> dict:
    return {
        "text": "fixture answer",
        "route": route or {"provider": "anthropic", "model": "fixture-model"},
        "usage": {"input_tokens": 6, "output_tokens": 4} if usage is _UNSET else usage,
    }


@pytest.mark.asyncio
async def test_bridge_records_sanitized_cancellation_with_operation_id(tmp_path, monkeypatch):
    profile = _profile(tmp_path)
    script = _executable(tmp_path, "print('{}')\n")
    provider = S2KBridgeProvider(
        (sys.executable, str(script)), str(profile), 2, 4096, operation_id="cancelled-operation"
    )
    events = []
    monkeypatch.setattr("app.llm.s2k_bridge.emit_event", lambda *args: events.append(args))

    async def cancel_communication(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr("app.llm.s2k_bridge._communicate_bounded", cancel_communication)
    with pytest.raises(asyncio.CancelledError):
        await provider.complete([{"role": "user", "content": "fixture"}])

    assert [event[1] for event in events] == ["bridge_started", "bridge_failed"]
    assert events[-1][3] == {
        "operation_id": "cancelled-operation",
        "error_type": "bridge_cancelled",
    }


@pytest.mark.parametrize(
    "timeout_seconds", [float("inf"), float("-inf"), float("nan"), 1 << 2048]
)
def test_bridge_rejects_non_finite_timeout(tmp_path, timeout_seconds):
    profile = _profile(tmp_path)
    script = _executable(tmp_path, "print('{}')\n")

    with pytest.raises(ValueError, match="timeout must be finite and positive"):
        S2KBridgeProvider((sys.executable, str(script)), str(profile), timeout_seconds, 4096)


@pytest.mark.parametrize(
    "operation_id", ["", "contains whitespace", "contains\nnewline", "x" * 129]
)
def test_bridge_rejects_unsafe_operation_identifier(tmp_path, operation_id):
    profile = _profile(tmp_path)
    script = _executable(tmp_path, "print('{}')\n")

    with pytest.raises(ValueError, match="operation ID must be a short safe identifier"):
        S2KBridgeProvider(
            (sys.executable, str(script)), str(profile), 2, 4096, operation_id=operation_id
        )


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
    provider = S2KBridgeProvider(
        (sys.executable, str(script)), str(profile), 2, 4096, operation_id="operation-123"
    )
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
        # Set by the child process itself, not passed by the bridge: macOS adds
        # __CF_USER_TEXT_ENCODING, and Python's C-locale coercion (PEP 538) adds
        # LC_CTYPE when the filtered environment carries no LANG/LC_ALL.
        "__CF_USER_TEXT_ENCODING",
        "LC_CTYPE",
    }
    assert usage == [
        {
            "input_tokens": 6,
            "output_tokens": 4,
            "model": "fixture-model",
            "provider": "anthropic",
            "credential_kind": "oauth",
            "tokens_available": True,
        }
    ]
    assert [event[1] for event in events] == ["bridge_started", "route_selected"]
    assert all(event[3]["operation_id"] == "operation-123" for event in events)
    uuid.UUID(events[-1][2])
    assert events[-1][3] == {
        "operation_id": "operation-123",
        "provider": "anthropic",
        "model": "fixture-model",
        "credential_kind": "oauth",
        "usage_status": "measured",
        "input_tokens": 6,
        "output_tokens": 4,
    }
    assert prompt not in json.dumps(events)


@pytest.mark.asyncio
async def test_unknown_usage_appends_an_unmeasured_entry_not_fabricated_tokens(tmp_path):
    profile = _profile(tmp_path)
    script = _executable(
        tmp_path,
        _READ_REQUEST + f"import json\nprint(json.dumps({_success(usage=None)!r}))\n",
    )
    provider = S2KBridgeProvider((sys.executable, str(script)), str(profile), 2, 4096)
    usage = []

    assert (
        await provider.complete([{"role": "user", "content": "fixture"}], usage_sink=usage)
        == "fixture answer"
    )
    assert usage == [
        {
            "input_tokens": 0,
            "output_tokens": 0,
            "model": "fixture-model",
            "provider": "anthropic",
            "credential_kind": "oauth",
            "tokens_available": False,
        }
    ]


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        ({"text": "answer", "usage": None}, "invalid_bridge_response"),
        (
            {"text": "answer", "route": {"provider": "openrouter", "model": "bad"}, "usage": None},
            "unapproved_route",
        ),
        (_success(usage={"input_tokens": 0, "output_tokens": 0}), "invalid_usage"),
        (_success(usage={"input_tokens": -1, "output_tokens": 2}), "invalid_usage"),
    ],
    ids=("absent-route", "unapproved-route", "fabricated-zero-usage", "negative-usage"),
)
@pytest.mark.asyncio
async def test_bridge_rejects_invalid_route_or_usage(tmp_path, response, kind):
    profile = _profile(tmp_path)
    script = _executable(
        tmp_path, _READ_REQUEST + f"import json\nprint(json.dumps({response!r}))\n"
    )
    provider = S2KBridgeProvider((sys.executable, str(script)), str(profile), 2, 4096)

    with pytest.raises(S2KBridgeError) as exc_info:
        await provider.complete([{"role": "user", "content": "fixture"}])

    assert exc_info.value.kind == kind


@pytest.mark.parametrize(
    ("body", "timeout_seconds", "kind"),
    [
        (_READ_REQUEST + "print('not-json')\n", 2, "invalid_bridge_json"),
        (
            _READ_REQUEST + "sys.stderr.write('PRIVATE_ERROR_OUTPUT')\nsys.exit(7)\n",
            2,
            "bridge_process_failed",
        ),
        (_READ_REQUEST + "print('x' * 200000)\n", 2, "stdout_too_large"),
        # Only the timeout case runs on a short budget: a child that has to start
        # inside 0.15s would turn every other case into bridge_timeout under load.
        ("import time\ntime.sleep(10)\n", 0.15, "bridge_timeout"),
    ],
    ids=("malformed-json", "nonzero-exit", "oversized-stdout", "timeout"),
)
@pytest.mark.asyncio
async def test_bridge_process_failures_are_bounded_and_sanitized(
    tmp_path, body, timeout_seconds, kind, monkeypatch
):
    profile = _profile(tmp_path)
    script = _executable(tmp_path, body)
    provider = S2KBridgeProvider(
        (sys.executable, str(script)),
        str(profile),
        timeout_seconds,
        4096,
        operation_id="failed-operation",
    )
    events = []
    monkeypatch.setattr("app.llm.s2k_bridge.emit_event", lambda *args: events.append(args))

    with pytest.raises(S2KBridgeError) as exc_info:
        await provider.complete([{"role": "user", "content": "PRIVATE_MODEL_OUTPUT"}])

    assert "PRIVATE" not in str(exc_info.value)
    assert "not-json" not in str(exc_info.value)
    assert "x" * 100 not in str(exc_info.value)
    assert exc_info.value.kind == kind
    assert [event[1] for event in events] == ["bridge_started", "bridge_failed"]
    assert events[-1][3] == {"operation_id": "failed-operation", "error_type": kind}


@pytest.mark.asyncio
async def test_bridge_propagates_sanitized_child_attempts_and_emits_event(tmp_path, monkeypatch):
    profile = _profile(tmp_path)
    script = _executable(
        tmp_path,
        "import json, sys\n"
        "request = json.load(sys.stdin)\n"
        "attempts = [\n"
        " {'provider': 'openai-codex', 'outcome': 'failed',\n"
        "  'error_type': 'provider_capacity', 'retryable': True},\n"
        " {'provider': 'anthropic', 'outcome': 'failed',\n"
        "  'error_type': 'provider_timeout', 'retryable': True},\n"
        "]\n"
        "error = {'type': 'providers_exhausted', 'retryable': True,\n"
        "         'attempts': attempts}\n"
        "print(json.dumps({'error': error, 'request_id': request['request_id']}))\n"
        "sys.exit(1)\n",
    )
    provider = S2KBridgeProvider(
        (sys.executable, str(script)), str(profile), 2, 4096, operation_id="failed-operation"
    )
    events = []
    monkeypatch.setattr("app.llm.s2k_bridge.emit_event", lambda *args: events.append(args))

    with pytest.raises(S2KBridgeError) as exc_info:
        await provider.complete([{"role": "user", "content": "fixture"}])

    error = exc_info.value
    assert error.kind == "providers_exhausted"
    assert error.retryable is True
    assert [attempt["provider"] for attempt in error.attempts] == ["openai-codex", "anthropic"]
    assert [event[1] for event in events] == ["bridge_started", "bridge_failed"]
    assert events[-1][3]["operation_id"] == "failed-operation"
    assert events[-1][3]["error_type"] == "providers_exhausted"
    assert events[-1][3]["attempts"] == error.attempts


def test_child_failure_rejects_attempts_without_matching_request_id():
    from app.llm.s2k_bridge import _child_failure

    payload = {
        "error": {
            "type": "providers_exhausted",
            "retryable": True,
            "attempts": [
                {
                    "provider": "openai-codex",
                    "outcome": "failed",
                    "error_type": "provider_capacity",
                    "retryable": True,
                }
            ],
        }
    }

    assert _child_failure(json.dumps(payload).encode(), "expected-request") is None


@pytest.mark.asyncio
async def test_bridge_rejects_oversized_stderr_and_reaps_child(tmp_path):
    profile = _profile(tmp_path)
    script = _executable(
        tmp_path, _READ_REQUEST + "sys.stderr.write('x' * 70000)\nprint('{}')\n"
    )
    provider = S2KBridgeProvider((sys.executable, str(script)), str(profile), 2, 4096)

    with pytest.raises(S2KBridgeError) as exc_info:
        await provider.complete([{"role": "user", "content": "fixture"}])

    assert exc_info.value.kind == "stderr_too_large"
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


async def _captured_request(tmp_path, task):
    capture = tmp_path / "request.json"
    script = _executable(
        tmp_path,
        "import json, sys\n"
        f"open({str(capture)!r}, 'w').write(sys.stdin.read())\n"
        "route = {'provider': 'openai-codex', 'model': 'm'}\n"
        "print(json.dumps({'text': 'ok', 'route': route, 'usage': None}))\n",
    )
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "config.yaml").write_text("auxiliary: {}\n", encoding="utf-8")
    kwargs = {} if task is None else {"task": task}
    provider = S2KBridgeProvider((sys.executable, str(script)), str(profile), 5, 4096, **kwargs)
    await provider.complete([{"role": "user", "content": "fixture"}])
    return json.loads(capture.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_s2k_requests_carry_no_task_field(tmp_path):
    assert "task" not in await _captured_request(tmp_path, None)


@pytest.mark.asyncio
async def test_decision_requests_carry_the_task(tmp_path):
    assert (await _captured_request(tmp_path, "decision"))["task"] == "decision"


def test_unknown_task_is_rejected(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "config.yaml").write_text("auxiliary: {}\n", encoding="utf-8")
    script = _executable(tmp_path, "print('{}')\n")
    with pytest.raises(ValueError, match="task"):
        S2KBridgeProvider((sys.executable, str(script)), str(profile), 5, 4096, task="analysis")


def test_bridge_provider_value_builds_the_decision_bridge(monkeypatch, tmp_path):
    import config
    from app.factory import _build_raw_llm_provider

    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "config.yaml").write_text("auxiliary: {}\n", encoding="utf-8")
    script = _executable(tmp_path, "print('{}')\n")
    monkeypatch.setattr(
        config,
        "settings",
        SimpleNamespace(
            LLM_PROVIDER="bridge",
            DECISION_COMPLETION_COMMAND="",
            DECISION_COMPLETION_PROFILE_HOME="",
            DECISION_COMPLETION_TIMEOUT_SECONDS=120.0,
            S2K_COMPLETION_COMMAND=f"{sys.executable} {script}",
            S2K_COMPLETION_PROFILE_HOME=str(profile),
            S2K_COMPLETION_MAX_STDOUT_BYTES=1_048_576,
        ),
    )
    provider = _build_raw_llm_provider()
    assert isinstance(provider, S2KBridgeProvider)
    assert provider.task == "decision"
    assert provider.completion_timeout_seconds == 120.0


def test_bridge_provider_value_fails_closed_without_a_command(monkeypatch):
    import config
    from app.factory import _build_raw_llm_provider

    monkeypatch.setattr(
        config,
        "settings",
        SimpleNamespace(
            LLM_PROVIDER="bridge",
            DECISION_COMPLETION_COMMAND="",
            DECISION_COMPLETION_PROFILE_HOME="",
            DECISION_COMPLETION_TIMEOUT_SECONDS=120.0,
            S2K_COMPLETION_COMMAND="",
            S2K_COMPLETION_PROFILE_HOME="",
            S2K_COMPLETION_MAX_STDOUT_BYTES=1_048_576,
        ),
    )
    with pytest.raises(ValueError, match="DECISION_COMPLETION_COMMAND"):
        _build_raw_llm_provider()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "usage", "kind", "available"),
    [
        ("openai-codex", {"input_tokens": 5, "output_tokens": 2}, "oauth", True),
        ("openai", {"input_tokens": 5, "output_tokens": 2}, "api_key", True),
        ("anthropic", None, "oauth", False),
    ],
)
async def test_bridge_usage_entry_names_route_and_credential(
    tmp_path, provider, usage, kind, available
):
    script = _executable(
        tmp_path,
        _READ_REQUEST
        + "import json\n"
        f"route = {{'provider': {provider!r}, 'model': 'm'}}\n"
        f"print(json.dumps({{'text': 'ok', 'route': route, 'usage': {usage!r}}}))\n",
    )
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "config.yaml").write_text("auxiliary: {}\n", encoding="utf-8")
    sink: list = []
    bridge = S2KBridgeProvider(
        (sys.executable, str(script)), str(profile), 5, 4096, task="decision"
    )
    await bridge.complete([{"role": "user", "content": "fixture"}], usage_sink=sink)
    (entry,) = sink
    observed = (
        entry["provider"], entry["model"], entry["credential_kind"], entry["tokens_available"]
    )
    assert observed == (provider, "m", kind, available)


def _failure_with_attempt(**extra):
    attempt = {
        "provider": "openai-codex",
        "outcome": "failed",
        "error_type": "provider_request_failed",
        "retryable": False,
        **extra,
    }
    return json.dumps({
        "error": {"type": "provider_request_failed", "retryable": False, "attempts": [attempt]},
        "request_id": "req-1",
    }).encode()


def test_child_failure_keeps_a_sanitized_attempt_cause():
    from app.llm.s2k_bridge import _child_failure

    cause = {"exception": "openai.BadRequestError", "status": 400}
    kind, retryable, attempts = _child_failure(_failure_with_attempt(cause=cause), "req-1")
    assert (kind, retryable) == ("provider_request_failed", False)
    assert attempts[0]["cause"] == cause

    no_status = {"exception": "RuntimeError", "status": None}
    _, _, attempts = _child_failure(_failure_with_attempt(cause=no_status), "req-1")
    assert attempts[0]["cause"] == no_status


def test_child_failure_without_a_cause_is_unchanged():
    from app.llm.s2k_bridge import _child_failure

    _, _, attempts = _child_failure(_failure_with_attempt(), "req-1")
    assert "cause" not in attempts[0]


@pytest.mark.parametrize(
    "cause",
    [
        {"exception": "Bad Request: prompt text here", "status": 400},
        {"exception": "x" * 129, "status": 400},
        {"exception": "", "status": 400},
        {"exception": "RuntimeError", "status": 99},
        {"exception": "RuntimeError", "status": True},
        {"exception": "RuntimeError", "status": "400"},
        {"exception": "RuntimeError"},
        {"exception": "RuntimeError", "status": 400, "message": "secret"},
        "RuntimeError",
    ],
)
def test_child_failure_rejects_an_unsafe_cause(cause):
    from app.llm.s2k_bridge import _child_failure

    assert _child_failure(_failure_with_attempt(cause=cause), "req-1") is None


def test_a_selected_attempt_cannot_carry_a_cause():
    from app.llm.s2k_bridge import _validated_attempts

    selected = {"provider": "openai-codex", "outcome": "selected", "error_type": None,
                "retryable": False, "cause": {"exception": "RuntimeError", "status": None}}
    assert _validated_attempts([selected]) is None
