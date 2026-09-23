"""Run the isolated Hermes S2K completion bridge as a bounded subprocess."""

from __future__ import annotations

import asyncio
import json
import math
import os
import shlex
import uuid
from collections.abc import Sequence
from pathlib import Path

from app.llm.protocol import LLMProvider, Message, Usage
from app.logging import emit_event

_ALLOWED_PROVIDERS = {"openai-codex", "anthropic", "openai"}
_MAX_STDERR_BYTES = 65_536


class S2KBridgeError(RuntimeError):
    """Sanitized subprocess/response error with no child output attached."""

    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(kind)


class _OutputLimitExceeded(Exception):
    def __init__(self, kind: str):
        self.kind = kind


async def _read_bounded(stream: asyncio.StreamReader, limit: int, kind: str) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await stream.read(min(65_536, limit - size + 1))
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > limit:
            raise _OutputLimitExceeded(kind)
        chunks.append(chunk)


async def _write_request(stream: asyncio.StreamWriter, payload: bytes) -> None:
    stream.write(payload)
    await stream.drain()
    stream.close()
    await stream.wait_closed()


def _close_pipe(process: asyncio.subprocess.Process, fd: int) -> None:
    transport = getattr(process, "_transport", None)
    get_pipe_transport = getattr(transport, "get_pipe_transport", None)
    pipe = get_pipe_transport(fd) if callable(get_pipe_transport) else None
    if pipe is not None:
        pipe.close()


async def _communicate_bounded(
    command: tuple[str, ...], payload: bytes, env: dict[str, str], stdout_limit: int
) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_task = asyncio.create_task(
        _read_bounded(process.stdout, stdout_limit, "stdout_too_large")
    )
    stderr_task = asyncio.create_task(
        _read_bounded(process.stderr, _MAX_STDERR_BYTES, "stderr_too_large")
    )
    stdin_task = asyncio.create_task(_write_request(process.stdin, payload))
    tasks = (stdin_task, stdout_task, stderr_task)
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            exception = task.exception()
            if exception is not None:
                raise exception
        _stdin_done, stdout, _stderr = await asyncio.gather(*tasks)
        returncode = await process.wait()
        return returncode, stdout
    except BaseException:
        if process.returncode is None:
            process.kill()
        process.stdin.close()
        _close_pipe(process, 1)
        _close_pipe(process, 2)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), timeout=1)
        except TimeoutError:
            pass
        raise


def _minimal_environment(profile_home: str) -> dict[str, str]:
    allowed = ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP")
    env = {name: os.environ[name] for name in allowed if os.environ.get(name)}
    env["HOME"] = env.get("HOME") or profile_home
    env["PATH"] = env.get("PATH") or "/usr/bin:/bin"
    env["HERMES_HOME"] = profile_home
    return env


class S2KBridgeProvider:
    """A provider-neutral Engine adapter to the isolated S2K bridge protocol."""

    def __init__(
        self,
        command: Sequence[str],
        profile_home: str,
        timeout_seconds: float,
        max_stdout_bytes: int,
    ) -> None:
        self._command = tuple(command)
        if len(self._command) != 2:
            raise ValueError(
                "S2K bridge command must contain an executable and one static script path"
            )
        executable, script = (Path(part) for part in self._command)
        if (
            not executable.is_absolute()
            or not executable.is_file()
            or not os.access(executable, os.X_OK)
        ):
            raise ValueError("S2K bridge executable must be an absolute executable path")
        if not script.is_absolute() or not script.is_file():
            raise ValueError("S2K bridge script must be an absolute existing path")

        profile = Path(profile_home)
        if (
            not profile.is_absolute()
            or not profile.is_dir()
            or not (profile / "config.yaml").is_file()
        ):
            raise ValueError("S2K bridge profile home must contain an isolated config.yaml")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            raise ValueError("S2K bridge timeout must be positive")
        if (
            isinstance(max_stdout_bytes, bool)
            or max_stdout_bytes <= 0
            or max_stdout_bytes > 1_048_576
        ):
            raise ValueError("S2K bridge stdout limit must be between 1 and 1048576 bytes")

        self._profile_home = str(profile)
        self._timeout_seconds = float(timeout_seconds)
        self._max_stdout_bytes = max_stdout_bytes

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 2048,
        temperature: float | None = None,
        usage_sink: list[Usage] | None = None,
    ) -> str:
        if model is not None:
            raise ValueError("S2K route models are selected by the isolated profile")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
            raise ValueError("max_tokens must be a positive integer")
        if temperature is not None and (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or not math.isfinite(temperature)
        ):
            raise ValueError("temperature must be a number or null")
        request_id = str(uuid.uuid4())
        try:
            payload = json.dumps(
                {
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "request_id": request_id,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise ValueError("S2K request is not JSON serializable") from None

        try:
            returncode, stdout = await asyncio.wait_for(
                _communicate_bounded(
                    self._command,
                    payload,
                    _minimal_environment(self._profile_home),
                    self._max_stdout_bytes,
                ),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            raise S2KBridgeError("bridge_timeout") from None
        except _OutputLimitExceeded as exc:
            raise S2KBridgeError(exc.kind) from None
        except Exception:
            raise S2KBridgeError("bridge_process_failed") from None
        if returncode != 0:
            raise S2KBridgeError("bridge_process_failed")
        if len(stdout) > self._max_stdout_bytes:
            raise S2KBridgeError("stdout_too_large")
        try:
            response = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise S2KBridgeError("invalid_bridge_json") from None
        if not isinstance(response, dict) or set(response) != {"text", "route", "usage"}:
            raise S2KBridgeError("invalid_bridge_response")
        text = response.get("text")
        route = response.get("route")
        if (
            not isinstance(text, str)
            or not text.strip()
            or not isinstance(route, dict)
            or set(route) != {"provider", "model"}
        ):
            raise S2KBridgeError("invalid_bridge_response")
        provider = route.get("provider")
        resolved_model = route.get("model")
        if (
            not isinstance(provider, str)
            or provider not in _ALLOWED_PROVIDERS
            or not isinstance(resolved_model, str)
            or not resolved_model.strip()
        ):
            raise S2KBridgeError("unapproved_route")

        usage = response.get("usage")
        measured: dict[str, int] | None = None
        if usage is not None:
            if not isinstance(usage, dict) or set(usage) != {"input_tokens", "output_tokens"}:
                raise S2KBridgeError("invalid_usage")
            input_tokens, output_tokens = usage["input_tokens"], usage["output_tokens"]
            if (
                isinstance(input_tokens, bool)
                or isinstance(output_tokens, bool)
                or not isinstance(input_tokens, int)
                or not isinstance(output_tokens, int)
                or input_tokens < 0
                or output_tokens < 0
                or input_tokens + output_tokens == 0
            ):
                raise S2KBridgeError("invalid_usage")
            measured = {"input_tokens": input_tokens, "output_tokens": output_tokens}

        if measured is not None and usage_sink is not None:
            usage_sink.append(
                {
                    "input_tokens": measured["input_tokens"],
                    "output_tokens": measured["output_tokens"],
                    "model": resolved_model,
                    "provider": provider,
                    "tokens_available": True,
                }
            )
        try:
            emit_event(
                "s2k_inference",
                "route_selected",
                request_id,
                {
                    "provider": provider,
                    "model": resolved_model,
                    "usage_status": "measured" if measured is not None else "unknown",
                },
            )
        except Exception:
            # Telemetry failure must not cause a paid request to be retried by its caller.
            pass
        return text


def build_s2k_llm_provider() -> LLMProvider:
    from config import settings

    command_text = settings.S2K_COMPLETION_COMMAND
    profile_home = settings.S2K_COMPLETION_PROFILE_HOME
    if not command_text or not profile_home:
        raise ValueError("S2K_COMPLETION_COMMAND and S2K_COMPLETION_PROFILE_HOME are required")
    try:
        command = shlex.split(command_text)
    except ValueError:
        raise ValueError("S2K_COMPLETION_COMMAND is malformed") from None
    return S2KBridgeProvider(
        command=command,
        profile_home=profile_home,
        timeout_seconds=settings.S2K_COMPLETION_TIMEOUT_SECONDS,
        max_stdout_bytes=settings.S2K_COMPLETION_MAX_STDOUT_BYTES,
    )
