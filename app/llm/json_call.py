"""Shared LLM JSON call with bounded repair retry.

All stages and persona agents expect a single JSON object back from the LLM.
A malformed response used to fail the run immediately; this module re-prompts
the model with the parse error instead, up to MAX_REPAIR_ATTEMPTS times, then
fails with the original semantics (json.JSONDecodeError propagates).
"""

from __future__ import annotations

import json
from typing import Any

from app.llm.protocol import LLMProvider, Message, Usage
from app.logging import emit_event

MAX_REPAIR_ATTEMPTS = 2

_REPAIR_INSTRUCTION = (
    "Your previous response was not valid JSON (parse error: {error}). "
    "Respond again with ONLY the corrected JSON object — no markdown fences, "
    "no commentary, no text before or after the JSON."
)


def parse_json(raw: str) -> dict[str, Any]:
    """Parse an LLM response as JSON, stripping markdown code fences if present."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return json.loads(text)


async def complete_json(
    llm: LLMProvider,
    messages: list[Message],
    *,
    stage: str,
    run_id: str,
    max_repair_attempts: int = MAX_REPAIR_ATTEMPTS,
    usage_sink: list[Usage] | None = None,
    **llm_kwargs,
) -> dict[str, Any]:
    """Call the LLM and parse its response as JSON, repairing on parse failure.

    On json.JSONDecodeError, re-prompts with the prior raw output and the parse
    error appended to the conversation. After max_repair_attempts failed
    repairs, the final error propagates (same failure semantics as before).

    If ``usage_sink`` is provided, every underlying LLM call (including each
    JSON-repair retry) appends its token usage — so the caller sees the true total
    cost of producing this stage's JSON, not just the final attempt.
    """
    raw = await llm.complete(messages=messages, usage_sink=usage_sink, **llm_kwargs)
    for attempt in range(1, max_repair_attempts + 1):
        try:
            return parse_json(raw)
        except json.JSONDecodeError as exc:
            emit_event(
                stage,
                "json_repair_attempt",
                run_id,
                {"attempt": attempt, "max_attempts": max_repair_attempts, "error": str(exc)},
            )
            repair_messages: list[Message] = [
                *messages,
                {"role": "assistant", "content": raw},
                {"role": "user", "content": _REPAIR_INSTRUCTION.format(error=exc)},
            ]
            raw = await llm.complete(messages=repair_messages, usage_sink=usage_sink, **llm_kwargs)
    try:
        return parse_json(raw)
    except json.JSONDecodeError as exc:
        emit_event(
            stage,
            "json_repair_failed",
            run_id,
            {"attempts": max_repair_attempts, "error": str(exc)},
        )
        raise
