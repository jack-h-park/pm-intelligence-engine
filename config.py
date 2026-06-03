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

    # Auto-triage: runs with relevance_score strictly below this threshold are
    # automatically completed as 'file' mode without pausing at Gate 1.
    # Range 1–5. Default 3 means scores 1–2 are auto-triaged; score 3+ goes to PM.
    AUTO_TRIAGE_THRESHOLD: int = 3
    # Transitional cutover switch. While True, pm-platform still writes the
    # legacy local wiki archive for auto-triaged signals. Set to False once
    # Hermes has taken over auto-triage archive ownership.
    AUTO_TRIAGE_LOCAL_ARCHIVE_ENABLED: bool = True

    # Base URL used to generate review page links sent in notifications.
    # Set to your server's public URL when deployed; default is local dev.
    BASE_URL: str = "http://localhost:8000"

    # Notifications — Gate 1 and Gate 2 alerts.
    # Leave a field empty ("") to disable that provider.
    # Both providers can be active simultaneously.
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    SLACK_WEBHOOK_URL: str = ""  # Incoming Webhook URL from Slack App settings

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
