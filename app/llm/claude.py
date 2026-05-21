from app.llm.protocol import Message


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

        response = await self._client.messages.create(**kwargs)
        return response.content[0].text
