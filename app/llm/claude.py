from typing import Any

from app.llm.protocol import Message, Usage
from app.llm.retry import with_retries


def _is_retryable(exc: Exception) -> bool:
    from anthropic import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError

    return isinstance(
        exc, RateLimitError | APIConnectionError | APITimeoutError | InternalServerError
    )


class ClaudeProvider:
    def __init__(self, api_key: str, default_model: str = "claude-sonnet-5") -> None:
        try:
            from anthropic import AsyncAnthropic
        except ImportError as e:
            raise ImportError("Install the 'anthropic' extra: pip install -e '.[anthropic]'") from e
        self._client = AsyncAnthropic(api_key=api_key)
        self._default_model = default_model

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 2048,
        temperature: float | None = None,
        usage_sink: list[Usage] | None = None,
    ) -> str:
        system_parts = [m["content"] for m in messages if m["role"] == "system"]
        non_system = [m for m in messages if m["role"] != "system"]

        kwargs: dict[str, Any] = {
            "model": model or self._default_model,
            "max_tokens": max_tokens,
            "messages": non_system,
        }
        if system_parts:
            kwargs["system"] = "\n\n".join(system_parts)
        # `temperature` is not forwarded: the configured default model
        # (claude-sonnet-5) rejects it outright — "`temperature` is deprecated
        # for this model" — so passing the caller's value here would 400 every
        # call. Pin ANTHROPIC_MODEL to a temperature-accepting snapshot (Sonnet
        # or Opus 4.6) before relying on `temperature` again.

        async def _call() -> str:
            response = await self._client.messages.create(**kwargs)
            if usage_sink is not None and response.usage is not None:
                usage_sink.append(
                    {
                        "input_tokens": response.usage.input_tokens or 0,
                        "output_tokens": response.usage.output_tokens or 0,
                        "model": response.model if hasattr(response, "model") else kwargs["model"],
                        "provider": "anthropic",
                    }
                )
            # Adaptive thinking (on by default on current-generation models) puts
            # a `thinking` block before the `text` block, so content[0] is not
            # reliably the answer — scan for the text block instead.
            for block in response.content:
                if block.type == "text":
                    text = block.text
                    if not isinstance(text, str):
                        raise TypeError(f"Anthropic returned a non-string text: {type(text)!r}")
                    return text
            raise ValueError("Anthropic response contained no text block")

        return await with_retries(_call, _is_retryable, "Anthropic")
