"""Provider-level regressions for the current-generation model migration.

Current-gen models reject `temperature` (ClaudeProvider) or reject it only on
reasoning-tier variants (OpenAIProvider), and Claude's adaptive thinking puts a
`thinking` block before the answer's `text` block. Both break silently unless
covered directly, since neither surfaces as a JSON-repair or retry failure.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.llm.claude import ClaudeProvider
from app.llm.openai import OpenAIProvider

_MESSAGES = [{"role": "user", "content": "hi"}]


def _anthropic_response(content: list, usage=None):
    return SimpleNamespace(content=content, usage=usage)


def _bad_request_error(body: dict):
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

    result = await provider.complete(_MESSAGES, temperature=0)

    assert result == "ok"
    assert mock_create.call_count == 2
    assert "temperature" in mock_create.call_args_list[0].kwargs
    assert "temperature" not in mock_create.call_args_list[1].kwargs


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
