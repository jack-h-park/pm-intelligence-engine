from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    DECISION_SYSTEM_ROOT: str = "/Users/jackpark/workspace/ai-assets/decision-context-companion-repo"
    WIKI_ROOT: str = "/Users/jackpark/workspace/ai-assets/product-management-wiki-repo"

    LLM_PROVIDER: str = "claude"
    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-sonnet-4-6"
    OPENAI_MODEL: str = "gpt-4o"

    DATABASE_URL: str = "sqlite:///./pm_platform.db"


settings = Settings()
