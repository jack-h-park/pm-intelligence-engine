from types import SimpleNamespace

import pytest


def _settings(
    provider: str = "claude",
    anthropic_model: str = "claude-sonnet-4-6",
    openai_model: str = "gpt-4o",
) -> SimpleNamespace:
    return SimpleNamespace(
        LLM_PROVIDER=provider,
        ANTHROPIC_MODEL=anthropic_model,
        OPENAI_MODEL=openai_model,
    )


def test_resolve_eval_model_name_uses_provider_specific_model():
    from eval.runner import resolve_eval_model_name

    assert resolve_eval_model_name(_settings(provider="claude")) == "claude-sonnet-4-6"
    assert resolve_eval_model_name(_settings(provider="openai")) == "gpt-4o"


def test_resolve_eval_runtime_includes_provider_and_model():
    from eval.runner import resolve_eval_runtime

    assert resolve_eval_runtime(_settings(provider="claude")) == ("claude", "claude-sonnet-4-6")
    assert resolve_eval_runtime(_settings(provider="openai")) == ("openai", "gpt-4o")


def test_resolve_eval_model_name_rejects_unsupported_provider():
    from eval.runner import resolve_eval_model_name

    with pytest.raises(ValueError, match="Unsupported LLM_PROVIDER"):
        resolve_eval_model_name(_settings(provider="gemini"))
