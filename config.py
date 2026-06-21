from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DECISION_CONTEXT_ROOT = "/Users/jackpark/workspace/ai-assets/decision-context-companion-repo"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DECISION_CONTEXT_ROOT: str = DEFAULT_DECISION_CONTEXT_ROOT
    DECISION_SYSTEM_ROOT: str | None = None
    WIKI_ROOT: str = "/Users/jackpark/workspace/ai-assets/product-management-wiki-repo"

    LLM_PROVIDER: str = "claude"
    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-sonnet-4-6"
    OPENAI_MODEL: str = "gpt-4o"

    DATABASE_URL: str = "sqlite:///./pm_platform.db"

    # Server-side API authentication. Every endpoint except GET /health requires
    # an `Authorization: Bearer <PM_PLATFORM_API_TOKEN>` header. The same token is
    # provisioned in the Hermes client `.env`. If left empty, the server fails
    # closed (503 on all authenticated routes) rather than serving an open API.
    PM_PLATFORM_API_TOKEN: str = ""

    # Auto-triage: runs with relevance_score strictly below this threshold are
    # automatically completed as 'file' mode without pausing at Gate 1.
    # Range 1–5. Default 3 means scores 1–2 are auto-triaged; score 3+ goes to PM.
    AUTO_TRIAGE_THRESHOLD: int = 3
    # Portfolio Triage fan-out cutoff (US-49). A product receives a full run only
    # when the signal scores at or above this relevance for that product. Range
    # 1–5. Default 4 ("clearly relevant") is stricter than auto-triage — fan-out
    # should reach only products with a real, actionable implication.
    TRIAGE_RELEVANCE_THRESHOLD: int = 4
    # Transitional cutover switch. While True, pm-engine still writes the
    # legacy local wiki archive for auto-triaged signals. Set to False once
    # Hermes has taken over auto-triage archive ownership.
    AUTO_TRIAGE_LOCAL_ARCHIVE_ENABLED: bool = True

    # Blocking-assumption verifier (US-42): after S5 classifies assumptions, a
    # second adversarial LLM pass re-applies the strict two-question test to each
    # Blocking and downgrades to Adjusting when a plausible alternative path
    # exists. Reduces false Kills from over-eager Blocking classification.
    # Opt-in (default False): doubles S5 LLM calls and is an unvalidated
    # heuristic — enable only after an eval confirms it preserves genuine Kills
    # (e.g. R06) while correcting over-flags. See ROADMAP US-42.
    BLOCKING_VERIFIER_ENABLED: bool = False

    # Retry cap. A failed run returns its signal to the `new` pool, where the
    # external poller picks it up and starts a fresh run. Without a cap, a
    # deterministically-failing signal (bad data, a code bug) would re-run
    # forever. Once a lineage reaches this many attempts, the signal is parked
    # as `blocked` instead of `new` so the loop stops until a human intervenes.
    # Counts total attempts, so 3 == original + 2 retries.
    MAX_RUN_ATTEMPTS: int = 3

    # Base URL used to generate review page links sent in notifications.
    # Set to your server's public URL when deployed; default is local dev.
    BASE_URL: str = "http://localhost:8000"

    # Notifications — Gate 1/2/3 alerts.
    # Leave a field empty ("") to disable that provider.
    # Both providers can be active simultaneously.
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    SLACK_WEBHOOK_URL: str = ""  # Incoming Webhook URL from Slack App settings

    # Master switch for pm-engine's built-in gate push (US-48). Default True
    # (local/dev/test). Set False on the iMac to hand gate + terminal messaging
    # to Hermes-ops (which polls the gate/terminal queues and composes
    # conversational messages). When False, build_notifier() wires no providers
    # so all send_gate1/2/3 calls are no-ops. Reversible — flip + restart.
    GATE_NOTIFICATIONS_ENABLED: bool = True

    # Path to gate0-state.json owned by the Hermes ops profile. When set, POST
    # /signals rejects any request whose source_ref matches a filename already in
    # the `skipped` bucket — preventing file_watch or other callers from creating
    # a live signal record for a file that PM has explicitly triaged out. Empty
    # string (default) disables the check; the engine is permissive.
    GATE0_STATE_FILE: str = ""

    @model_validator(mode="after")
    def _resolve_decision_root_aliases(self) -> "Settings":
        if self.DECISION_CONTEXT_ROOT != DEFAULT_DECISION_CONTEXT_ROOT:
            self.DECISION_SYSTEM_ROOT = self.DECISION_CONTEXT_ROOT
            return self
        if self.DECISION_SYSTEM_ROOT:
            self.DECISION_CONTEXT_ROOT = self.DECISION_SYSTEM_ROOT
        self.DECISION_SYSTEM_ROOT = self.DECISION_CONTEXT_ROOT
        return self


settings = Settings()


# Product families (US-49 conservative fan-out). A routing/grouping layer only —
# NOT the workflow unit: per-product decision-context, the eval golden set, and the
# observatory schema are unchanged. Used to (1) pick the fan-out primary within the
# most-relevant family and (2) group deferred candidates so ops can offer "same
# family" promotions. See docs/MULTI_PRODUCT_SIGNAL_FANOUT.md §0.
PRODUCT_FAMILIES: dict[str, str] = {
    "example-security-product": "security-products",
    "example-mobile-product": "security-products",
    "example-governance-product": "security-products",
    "example-enterprise-ai-product": "security-products",
    "example-identity-product": "identity-products",
    "example-consumer-product": "consumer-products",
    "example-agent-product": "consumer-products",
}


def family_of(product_id: str) -> str:
    """The product's family, or "unassigned" for products not yet mapped."""
    return PRODUCT_FAMILIES.get(product_id, "unassigned")
