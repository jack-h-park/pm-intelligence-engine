import shlex
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
    context_loader = ContextLoader(settings.DECISION_SYSTEM_ROOT)
    template_service = TemplateService(settings.DECISION_SYSTEM_ROOT)
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


def build_insight_llm_provider() -> LLMProvider:
    """Build the isolated OAuth provider used only by personal Insight jobs."""
    from app.llm.hermes_oauth import HermesOAuthProvider
    from config import settings

    command = tuple(shlex.split(settings.INSIGHT_OAUTH_COMMAND))
    if not command:
        raise ValueError("INSIGHT_OAUTH_COMMAND is required for insight job execution")
    return HermesOAuthProvider(command=command, profile=settings.INSIGHT_OAUTH_PROFILE)
