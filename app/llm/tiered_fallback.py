"""Use the matching Claude tier after an exhausted transient OpenAI failure."""

from __future__ import annotations

import logging

from app.llm.openai import _is_retryable
from app.llm.protocol import LLMProvider, Message, Usage

_FALLBACK_MODELS = {
    "gpt-6-sol": "claude-sonnet-5",
    "gpt-6-luna": "claude-haiku-4-5-20251001",
}
_LOG = logging.getLogger(__name__)


class TieredFallbackProvider:
    def __init__(self, primary: LLMProvider, fallback: LLMProvider, *, default_model: str) -> None:
        self._primary = primary
        self._fallback = fallback
        self._default_model = default_model

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 2048,
        temperature: float | None = None,
        usage_sink: list[Usage] | None = None,
    ) -> str:
        selected_model = model or self._default_model
        try:
            return await self._primary.complete(
                messages,
                model=selected_model,
                max_tokens=max_tokens,
                temperature=temperature,
                usage_sink=usage_sink,
            )
        except Exception as exc:
            # OpenAIProvider retries transient failures and then raises a
            # RuntimeError chained from the original SDK exception. Never
            # fail over a bad request, authentication failure, or unknown model.
            cause = exc.__cause__ or exc
            fallback_model = _FALLBACK_MODELS.get(selected_model)
            if not fallback_model or not isinstance(cause, Exception) or not _is_retryable(cause):
                raise
            _LOG.warning(
                "OpenAI %s failed with %s; trying Anthropic %s",
                selected_model,
                type(cause).__name__,
                fallback_model,
            )
            return await self._fallback.complete(
                messages,
                model=fallback_model,
                max_tokens=max_tokens,
                temperature=temperature,
                usage_sink=usage_sink,
            )
