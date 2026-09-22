"""Provider-level regressions for the current-generation model migration.

Current-gen models reject `temperature` (ClaudeProvider) or reject it only on
reasoning-tier variants (OpenAIProvider), and Claude's adaptive thinking puts a
`thinking` block before the answer's `text` block. Both break silently unless
covered directly, since neither surfaces as a JSON-repair or retry failure.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from app.llm.claude import ClaudeProvider
from app.llm.openai import OpenAIProvider

_MESSAGES = [{"role": "user", "content": "hi"}]


def _anthropic_response(content: list[Any], usage=None):
    return SimpleNamespace(content=content, usage=usage)


def _bad_request_error(body: dict[str, Any]):
    from openai import BadRequestError

    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(400, request=request, json=body)
    return BadRequestError(message=body.get("message", ""), response=response, body=body)


@pytest.mark.asyncio
async def test_claude_provider_skips_leading_thinking_block():
    provider = ClaudeProvider(api_key="test-key")
    provider._client.messages.create = AsyncMock(
        return_value=_anthropic_response(
            [
                SimpleNamespace(type="thinking", thinking=""),
                SimpleNamespace(type="text", text="the answer"),
            ]
        )
    )

    result = await provider.complete(_MESSAGES, temperature=0)

    assert result == "the answer"


@pytest.mark.asyncio
async def test_claude_provider_does_not_forward_temperature():
    provider = ClaudeProvider(api_key="test-key")
    mock_create = AsyncMock(
        return_value=_anthropic_response([SimpleNamespace(type="text", text="ok")])
    )
    provider._client.messages.create = mock_create

    await provider.complete(_MESSAGES, temperature=0)

    assert "temperature" not in mock_create.call_args.kwargs


@pytest.mark.asyncio
async def test_claude_provider_raises_on_no_text_block():
    provider = ClaudeProvider(api_key="test-key")
    provider._client.messages.create = AsyncMock(
        return_value=_anthropic_response([SimpleNamespace(type="thinking", thinking="")])
    )

    with pytest.raises(ValueError, match="no text block"):
        await provider.complete(_MESSAGES)


@pytest.mark.asyncio
async def test_openai_provider_retries_without_temperature_on_unsupported_param():
    provider = OpenAIProvider(api_key="test-key")

    rejected = _bad_request_error(
        {
            "message": "Unsupported parameter: 'temperature' is not supported with this model.",
            "param": "temperature",
            "code": "unsupported_parameter",
            "type": "invalid_request_error",
        }
    )
    accepted = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
        usage=None,
    )
    mock_create = AsyncMock(side_effect=[rejected, accepted])
    provider._client.chat.completions.create = mock_create

    result = await provider.complete(_MESSAGES, model="gpt-5.6-terra", temperature=0)

    assert result == "ok"
    assert mock_create.call_count == 2
    assert "temperature" in mock_create.call_args_list[0].kwargs
    assert "temperature" not in mock_create.call_args_list[1].kwargs


@pytest.mark.asyncio
async def test_gpt6_provider_omits_unsupported_temperature_on_first_call():
    provider = OpenAIProvider(api_key="test-key")
    mock_create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=None,
        )
    )
    provider._client.chat.completions.create = mock_create

    assert await provider.complete(_MESSAGES, model="gpt-6-sol", temperature=0) == "ok"
    assert mock_create.call_count == 1
    assert "temperature" not in mock_create.call_args.kwargs


@pytest.mark.asyncio
async def test_openai_provider_reraises_unrelated_bad_request():
    from openai import BadRequestError

    provider = OpenAIProvider(api_key="test-key")

    rejected = _bad_request_error(
        {"message": "Invalid value for 'model'.", "param": "model", "code": "invalid_value"}
    )
    provider._client.chat.completions.create = AsyncMock(side_effect=rejected)

    with pytest.raises(BadRequestError):
        await provider.complete(_MESSAGES, temperature=0)


@pytest.mark.asyncio
async def test_provider_usage_records_the_effective_model():
    openai_provider = OpenAIProvider(api_key="test-key")
    openai_provider._client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=1),
        )
    )
    claude_provider = ClaudeProvider(api_key="test-key")
    claude_provider._client.messages.create = AsyncMock(
        return_value=_anthropic_response(
            [SimpleNamespace(type="text", text="ok")],
            SimpleNamespace(input_tokens=4, output_tokens=2),
        )
    )
    usage: list = []

    await openai_provider.complete(_MESSAGES, model="gpt-6-sol", usage_sink=usage)
    await claude_provider.complete(_MESSAGES, model="claude-sonnet-5", usage_sink=usage)

    assert [(u["model"], u["provider"]) for u in usage] == [
        ("gpt-6-sol", "openai"),
        ("claude-sonnet-5", "anthropic"),
    ]


@pytest.mark.asyncio
async def test_successful_fallback_model_is_recorded_when_usage_is_missing():
    from app.models.stages import StageMetadata

    provider = ClaudeProvider(api_key="test-key")
    provider._client.messages.create = AsyncMock(
        return_value=_anthropic_response([SimpleNamespace(type="text", text="ok")])
    )
    usage: list = []

    assert await provider.complete(_MESSAGES, model="claude-sonnet-5", usage_sink=usage) == "ok"
    metadata = StageMetadata.with_usage("gpt-6-sol", usage)
    assert metadata.model_used == "claude-sonnet-5"
    assert metadata.input_tokens is None
    assert metadata.output_tokens is None
