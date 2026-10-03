"""The requested GPT-6 tier keeps its corresponding Claude fallback."""

import httpx
import pytest

from app.llm.tiered_fallback import TieredFallbackProvider
from app.models.stages import StageMetadata


class Provider:
    def __init__(self, result: str = "ok", error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[str | None] = []

    async def complete(
        self, messages, model=None, max_tokens=2048, temperature=None, usage_sink=None
    ):
        self.calls.append(model)
        if self.error is not None:
            raise self.error
        if usage_sink is not None:
            usage_sink.append(
                {"input_tokens": 3, "output_tokens": 1, "model": model or "provider-default"}
            )
        return self.result


def _rate_limit_error():
    from openai import RateLimitError

    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(429, request=request, json={"error": {"message": "limited"}})
    return RateLimitError(message="limited", response=response, body=None)


@pytest.mark.parametrize(
    "primary_model,fallback_model",
    [
        ("gpt-6-sol", "claude-sonnet-5"),
        ("gpt-6-luna", "claude-haiku-4-5-20251001"),
    ],
)
@pytest.mark.asyncio
async def test_retryable_primary_failure_uses_matching_claude_model(primary_model, fallback_model):
    primary = Provider(error=RuntimeError("OpenAI request failed"))
    primary.error.__cause__ = _rate_limit_error()
    fallback = Provider(result="fallback answer")
    provider = TieredFallbackProvider(primary, fallback, default_model=primary_model)
    usage = []

    result = await provider.complete([{"role": "user", "content": "hi"}], usage_sink=usage)

    assert result == "fallback answer"
    assert primary.calls == [primary_model]
    assert fallback.calls == [fallback_model]
    assert StageMetadata.with_usage(primary_model, usage).model_used == fallback_model


@pytest.mark.asyncio
async def test_invalid_primary_request_does_not_fallback():
    primary = Provider(error=ValueError("invalid request"))
    fallback = Provider()
    provider = TieredFallbackProvider(primary, fallback, default_model="gpt-6-sol")

    with pytest.raises(ValueError, match="invalid request"):
        await provider.complete([{"role": "user", "content": "hi"}])
    assert fallback.calls == []


@pytest.mark.asyncio
async def test_unmapped_model_override_does_not_guess_a_fallback():
    primary = Provider(error=RuntimeError("OpenAI request failed"))
    primary.error.__cause__ = _rate_limit_error()
    fallback = Provider()
    provider = TieredFallbackProvider(primary, fallback, default_model="gpt-6-sol")

    with pytest.raises(RuntimeError, match="OpenAI request failed"):
        await provider.complete([{"role": "user", "content": "hi"}], model="gpt-5.6-terra")
    assert fallback.calls == []


def test_engine_defaults_select_sol_with_sonnet_fallback(monkeypatch):
    import config
    from app.factory import _build_raw_llm_provider

    settings = config.Settings(
        _env_file=None,
        LLM_PROVIDER="openai",
        OPENAI_API_KEY="test-openai",
        ANTHROPIC_API_KEY="test-anthropic",
    )
    monkeypatch.setattr(config, "settings", settings)

    provider = _build_raw_llm_provider()

    assert settings.OPENAI_MODEL == "gpt-6-sol"
    assert provider._default_model == "gpt-6-sol"
    assert provider._primary._default_model == "gpt-6-sol"
    assert provider._fallback._default_model == "claude-sonnet-5"


def _settings(monkeypatch, **overrides):
    import config

    settings = config.Settings(
        _env_file=None,
        OPENAI_API_KEY="test-openai",
        ANTHROPIC_API_KEY="test-anthropic",
        **overrides,
    )
    monkeypatch.setattr(config, "settings", settings)
    return settings


@pytest.mark.asyncio
@pytest.mark.parametrize("primary_model", ["gpt-6-sol", "gpt-6-luna", "gpt-custom"])
async def test_described_fallback_is_the_one_the_provider_takes(monkeypatch, primary_model):
    from app.factory import describe_llm_config

    _settings(monkeypatch, LLM_PROVIDER="openai", OPENAI_MODEL=primary_model)
    described = describe_llm_config()
    assert described["provider"] == "openai"
    assert described["credential"] == "api-key"
    assert described["model"] == primary_model

    primary = Provider(error=RuntimeError("OpenAI request failed"))
    primary.error.__cause__ = _rate_limit_error()
    fallback = Provider()
    provider = TieredFallbackProvider(primary, fallback, default_model=primary_model)
    try:
        await provider.complete([{"role": "user", "content": "hi"}])
    except RuntimeError:
        pass

    if described["fallback"] is None:
        assert fallback.calls == []
    else:
        assert described["fallback"]["provider"] == "anthropic"
        assert fallback.calls == [described["fallback"]["model"]]


def test_claude_provider_is_described_without_a_fallback(monkeypatch):
    from app.factory import describe_llm_config

    _settings(monkeypatch, LLM_PROVIDER="claude", ANTHROPIC_MODEL="claude-sonnet-5")

    described = describe_llm_config()

    assert described["model"] == "claude-sonnet-5"
    assert described["fallback"] is None


def test_s2k_bridge_is_described_by_profile_name_never_by_path(monkeypatch):
    from app.factory import describe_llm_config

    _settings(monkeypatch)
    assert describe_llm_config()["s2k_bridge"] == {"enabled": False, "profile": None}

    _settings(
        monkeypatch,
        S2K_COMPLETION_COMMAND="/usr/bin/python3 /opt/s2k_completion.py",
        S2K_COMPLETION_PROFILE_HOME="/Users/someone/.hermes/profiles/researcher",
    )
    described = describe_llm_config()
    assert described["s2k_bridge"] == {"enabled": True, "profile": "researcher"}
    assert "/Users/" not in str(described)


def test_openai_description_keeps_its_shape(monkeypatch):
    from app.factory import describe_llm_config

    _settings(monkeypatch, LLM_PROVIDER="openai", OPENAI_MODEL="gpt-6-sol")

    assert set(describe_llm_config()) == {
        "provider",
        "credential",
        "model",
        "fallback",
        "s2k_bridge",
    }


def test_bridge_provider_is_described_by_profile_name_for_the_decision_task(monkeypatch):
    from app.factory import describe_llm_config

    _settings(
        monkeypatch,
        LLM_PROVIDER="bridge",
        DECISION_COMPLETION_PROFILE_HOME="/Users/someone/.hermes/profiles/decider",
        S2K_COMPLETION_COMMAND="/usr/bin/python3 /opt/s2k_completion.py",
        S2K_COMPLETION_PROFILE_HOME="/Users/someone/.hermes/profiles/researcher",
    )
    described = describe_llm_config()

    assert described["provider"] == "bridge"
    assert described["model"] is None
    assert described["fallback"] is None
    assert described["credential"] == "hermes-profile"
    assert described["decision_bridge"] == {"profile": "decider", "task": "decision"}
    assert "/Users/" not in str(described)


def test_bridge_description_uses_the_s2k_profile_when_no_decision_profile_is_set(monkeypatch):
    from app.factory import describe_llm_config

    _settings(
        monkeypatch,
        LLM_PROVIDER="bridge",
        DECISION_COMPLETION_PROFILE_HOME="",
        S2K_COMPLETION_COMMAND="/usr/bin/python3 /opt/s2k_completion.py",
        S2K_COMPLETION_PROFILE_HOME="/Users/someone/.hermes/profiles/researcher",
    )

    assert describe_llm_config()["decision_bridge"] == {"profile": "researcher", "task": "decision"}
