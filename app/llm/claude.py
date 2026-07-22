from app.llm.protocol import Message, Usage
from app.llm.retry import with_retries


def _is_retryable(exc: Exception) -> bool:
    from anthropic import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError

    return isinstance(
        exc, RateLimitError | APIConnectionError | APITimeoutError | InternalServerError
    )


class ClaudeProvider:
    def __init__(self, api_key: str, default_model: str = "claude-sonnet-4-6") -> None:
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

        kwargs: dict = {
            "model": model or self._default_model,
            "max_tokens": max_tokens,
            "messages": non_system,
        }
        if system_parts:
            kwargs["system"] = "\n\n".join(system_parts)
        if temperature is not None:
            kwargs["temperature"] = temperature

        async def _call() -> str:
            response = await self._client.messages.create(**kwargs)
            if usage_sink is not None and response.usage is not None:
                usage_sink.append(
                    {
                        "input_tokens": response.usage.input_tokens or 0,
                        "output_tokens": response.usage.output_tokens or 0,
                    }
                )
            return response.content[0].text

        return await with_retries(_call, _is_retryable, "Anthropic")
