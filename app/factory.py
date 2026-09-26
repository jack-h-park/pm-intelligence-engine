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


def build_s2k_llm_provider(operation_id: str | None = None) -> LLMProvider:
    """Build the fixture-bounded S2K transport provider for scoped Insight work."""
    from app.llm.s2k_bridge import build_s2k_llm_provider as build_provider

    return build_provider(operation_id=operation_id)
