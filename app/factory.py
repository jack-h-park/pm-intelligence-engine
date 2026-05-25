from dataclasses import dataclass

from app.llm.protocol import LLMProvider
from app.services.context_loader import ContextLoader
from app.services.notifier import FanoutNotifier
from app.services.template_service import TemplateService
from app.storage.protocol import PMWorkflowStore


@dataclass
class PMEngine:
    store: PMWorkflowStore
    llm: LLMProvider
    context_loader: ContextLoader
    template_service: TemplateService
    notifier: FanoutNotifier


def build_engine(runtime: str = "local") -> PMEngine:
    from config import settings
    from app.services.notifier import build_notifier
    from app.storage.sqlite_store import SQLiteStore

    store: PMWorkflowStore = SQLiteStore(settings.DATABASE_URL)
    context_loader = ContextLoader(settings.DECISION_SYSTEM_ROOT)
    template_service = TemplateService(settings.DECISION_SYSTEM_ROOT)
    llm = _build_llm_provider()
    notifier = build_notifier()

    return PMEngine(
        store=store,
        llm=llm,
        context_loader=context_loader,
        template_service=template_service,
        notifier=notifier,
    )


def _build_llm_provider() -> LLMProvider:
    from config import settings

    provider = settings.LLM_PROVIDER.lower()
    if provider == "claude":
        from app.llm.claude import ClaudeProvider

        return ClaudeProvider(
            api_key=settings.ANTHROPIC_API_KEY,
            default_model=settings.ANTHROPIC_MODEL,
        )
    elif provider == "openai":
        from app.llm.openai import OpenAIProvider

        return OpenAIProvider(
            api_key=settings.OPENAI_API_KEY,
            default_model=settings.OPENAI_MODEL,
        )
    else:
        raise ValueError(
            f"Unknown LLM_PROVIDER '{provider}'. Set LLM_PROVIDER=claude or LLM_PROVIDER=openai"
        )
