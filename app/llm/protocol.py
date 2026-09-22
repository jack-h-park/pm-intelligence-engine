from typing import Literal, NotRequired, Protocol, TypedDict, runtime_checkable


class Message(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


class Usage(TypedDict):
    """Token usage for a single LLM call, provider-normalized."""

    input_tokens: int
    output_tokens: int
    model: NotRequired[str]
    provider: NotRequired[str]


@runtime_checkable
class LLMProvider(Protocol):
    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 2048,
        temperature: float | None = None,
        usage_sink: list["Usage"] | None = None,
    ) -> str:
        """Return the completion text.

        If ``usage_sink`` is provided, each successful underlying API call appends
        its normalized ``Usage`` to the list (one entry per call — JSON-repair
        retries append again, so the caller can sum the list for the true total).
        Passing ``None`` (the default) is fully backward-compatible.
        """
        ...
