"""OAuth-subscription LLM provider for bounded personal insight jobs."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from app.llm.protocol import Message, Usage


class HermesOAuthProvider:
    """Delegate one completion to the local Hermes OAuth CLI.

    The provider intentionally has no API-key parameter.  The CLI owns the
    authenticated subscription session selected by ``profile``.
    """

    def __init__(self, command: Sequence[str], profile: str) -> None:
        if not command:
            raise ValueError("OAuth CLI command is required")
        if not profile:
            raise ValueError("OAuth profile is required")
        self._command = tuple(command)
        self._profile = profile

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 2048,
        temperature: float | None = None,
        usage_sink: list[Usage] | None = None,
    ) -> str:
        del model, max_tokens, temperature
        prompt = "\n\n".join(f"{message['role']}: {message['content']}" for message in messages)
        process = await asyncio.create_subprocess_exec(
            *self._command,
            "--profile",
            self._profile,
            "-z",
            prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Hermes OAuth CLI failed ({process.returncode}): {detail}")
        text = stdout.decode("utf-8", errors="strict").strip()
        if not text:
            raise ValueError("Hermes OAuth CLI returned an empty completion")
        if usage_sink is not None:
            usage_sink.append({"input_tokens": 0, "output_tokens": 0})
        return text
