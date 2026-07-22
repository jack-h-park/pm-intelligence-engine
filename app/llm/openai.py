from app.llm.protocol import Message, Usage
from app.llm.retry import with_retries


def _is_retryable(exc: Exception) -> bool:
    from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError

    return isinstance(
        exc, RateLimitError | APIConnectionError | APITimeoutError | InternalServerError
    )


class OpenAIProvider:
    def __init__(self, api_key: str, default_model: str = "gpt-5.4") -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as e:
            raise ImportError("Install the 'openai' extra: pip install -e '.[openai]'") from e
        self._client = AsyncOpenAI(api_key=api_key)
        self._default_model = default_model

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 2048,
        temperature: float | None = None,
        usage_sink: list[Usage] | None = None,
    ) -> str:
        kwargs: dict = {
            "model": model or self._default_model,
            "max_completion_tokens": max_tokens,
            "messages": messages,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature

        async def _call() -> str:
            response = await self._client.chat.completions.create(
                **kwargs  # type: ignore[arg-type]
            )
            content = response.choices[0].message.content
            if content is None:
                raise ValueError("OpenAI returned an empty response")
            if usage_sink is not None and response.usage is not None:
                usage_sink.append(
                    {
                        "input_tokens": response.usage.prompt_tokens or 0,
                        "output_tokens": response.usage.completion_tokens or 0,
                    }
                )
            return content

        return await with_retries(_call, _is_retryable, "OpenAI")
