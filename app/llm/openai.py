import asyncio

from app.llm.protocol import Message, Usage

_RETRY_DELAYS = [5, 15, 30]  # seconds between retries on rate limit


class OpenAIProvider:
    def __init__(self, api_key: str, default_model: str = "gpt-4o") -> None:
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
        from openai import RateLimitError

        last_exc: Exception | None = None
        for attempt, delay in enumerate([0] + _RETRY_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            try:
                kwargs: dict = {
                    "model": model or self._default_model,
                    "max_completion_tokens": max_tokens,
                    "messages": messages,
                }
                if temperature is not None:
                    kwargs["temperature"] = temperature
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
            except RateLimitError as exc:
                last_exc = exc
                continue

        raise RuntimeError(
            f"OpenAI rate limit exceeded after {len(_RETRY_DELAYS) + 1} attempts"
        ) from last_exc
