from dataclasses import dataclass

from app.llm.protocol import LLMProvider
from app.services.context_loader import ContextLoader
from app.services.notifier import FanoutNotifier
from app.services.template_service import TemplateService
from app.storage.insight_store import InsightStore
from app.storage.protocol import PMWorkflowStore


@dataclass
class PMEngine:
    store: PMWorkflowStore
    llm: LLMProvider
    context_loader: ContextLoader
    template_service: TemplateService
    notifier: FanoutNotifier
    insight_store: InsightStore | None = None


def build_engine(runtime: str = "local") -> PMEngine:
    from app.services.notifier import build_notifier
    from app.storage.sqlite_store import SQLiteStore
    from config import settings

    store: PMWorkflowStore = SQLiteStore(settings.DATABASE_URL)
    insight_store = InsightStore(settings.DATABASE_URL)
    context_loader = ContextLoader(settings.decision_system_root)
    template_service = TemplateService(settings.decision_system_root)
    llm = _build_llm_provider()
    notifier = build_notifier()

    return PMEngine(
        store=store,
        insight_store=insight_store,
        llm=llm,
        context_loader=context_loader,
        template_service=template_service,
        notifier=notifier,
    )


def _build_llm_provider() -> LLMProvider:
    """Build the configured provider, wrapped for tracing.

    The wrapper is applied here rather than inside each provider so that every
    implementation — including ones added later — is instrumented once. It is
    transparent when tracing is off.
    """
    from app.telemetry import TracingLLMProvider

    return TracingLLMProvider(_build_raw_llm_provider())


def _build_raw_llm_provider() -> LLMProvider:
    from config import settings

    provider = settings.LLM_PROVIDER.lower()
    if provider == "claude":
        from app.llm.claude import ClaudeProvider

        return ClaudeProvider(
            api_key=settings.ANTHROPIC_API_KEY,
            default_model=settings.ANTHROPIC_MODEL,
        )
    elif provider == "openai":
        from app.llm.claude import ClaudeProvider
        from app.llm.openai import OpenAIProvider
        from app.llm.tiered_fallback import TieredFallbackProvider

        if not settings.ANTHROPIC_API_KEY:
            raise ValueError("ANTHROPIC_API_KEY is required for the OpenAI fallback chain")
        primary = OpenAIProvider(
            api_key=settings.OPENAI_API_KEY,
            default_model=settings.OPENAI_MODEL,
        )
        fallback = ClaudeProvider(
            api_key=settings.ANTHROPIC_API_KEY,
            default_model=settings.ANTHROPIC_MODEL,
        )
        return TieredFallbackProvider(primary, fallback, default_model=settings.OPENAI_MODEL)
    else:
        raise ValueError(
            f"Unknown LLM_PROVIDER '{provider}'. Set LLM_PROVIDER=claude or LLM_PROVIDER=openai"
        )


def describe_llm_config() -> dict[str, object]:
    """The LLM call path this process was configured with, for GET /health.

    Declared values only — never a key. The console renders this beside the
    Hermes profiles' chains so the engine's own API-key path is visible in the
    same place; it reads it from here rather than restating the fallback map.
    """
    from pathlib import Path

    from app.llm.tiered_fallback import _FALLBACK_MODELS
    from config import settings

    provider = settings.LLM_PROVIDER.lower()
    if provider == "claude":
        model, fallback = settings.ANTHROPIC_MODEL, None
    else:
        model = settings.OPENAI_MODEL
        fallback_model = _FALLBACK_MODELS.get(model)
        fallback = (
            {
                "provider": "anthropic",
                "model": fallback_model,
                "on": "transient failure after retries",
            }
            if fallback_model
            else None
        )
    s2k_home = settings.S2K_COMPLETION_PROFILE_HOME
    return {
        "provider": provider,
        "credential": "api-key",
        "model": model,
        "fallback": fallback,
        "s2k_bridge": {
            "enabled": bool(settings.S2K_COMPLETION_COMMAND and s2k_home),
            "profile": Path(s2k_home).name if s2k_home else None,
        },
    }


def build_s2k_llm_provider(operation_id: str | None = None) -> LLMProvider:
    """Build the fixture-bounded S2K transport provider for scoped Insight work.

    Wrapped for tracing at this single construction point, like the configured
    provider above. The span measures the engine's side of the subprocess call;
    callers that need the bridge itself use ``telemetry.unwrap_provider``.
    """
    from app.llm.s2k_bridge import build_s2k_llm_provider as build_provider
    from app.telemetry import TracingLLMProvider

    return TracingLLMProvider(build_provider(operation_id=operation_id))
