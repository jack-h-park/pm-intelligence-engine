from app.llm.protocol import Message


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
    ) -> str:
        from openai import AsyncOpenAI  # already imported in __init__, re-import for type checker

        response = await self._client.chat.completions.create(
            model=model or self._default_model,
            max_tokens=max_tokens,
            messages=messages,  # type: ignore[arg-type]
        )
        content = response.choices[0].message.content
        if content is None:
            raise ValueError("OpenAI returned an empty response")
        return content
