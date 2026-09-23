from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Companion context is configured by the operator. A relative default keeps a
# fresh clone self-contained and avoids embedding a host path.
DEFAULT_DECISION_CONTEXT_ROOT = "./decision-context"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DECISION_CONTEXT_ROOT: str = DEFAULT_DECISION_CONTEXT_ROOT
    DECISION_SYSTEM_ROOT: str | None = None
    WIKI_ROOT: str = "./wiki"

    # The `source:` value stamped into every wiki-sync frontmatter (see
    # wiki_sync.py's build_frontmatter). The wiki repo's own schema is what
    # actually constrains this — if yours expects a specific producer name,
    # set it here; the default is just this engine's own name.
    WIKI_SOURCE_TAG: str = "pm-intelligence-engine"

    # These defaults must match the deployed `.env`. A default that lags the live
    # value does not fail — it silently runs a different provider or an older
    # model, and only `metadata.model_used` on a finished run reveals it.
    LLM_PROVIDER: str = "openai"
    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-sonnet-5"
    OPENAI_MODEL: str = "gpt-6-sol"
    # S2K completions use a separate bounded control-plane bridge subprocess.
    # These remain unset until the isolated profile and absolute bridge command
    # have been provisioned; the factory fails closed when either is missing.
    S2K_COMPLETION_COMMAND: str = ""
    S2K_COMPLETION_PROFILE_HOME: str = ""
    S2K_COMPLETION_TIMEOUT_SECONDS: float = 90.0
    S2K_COMPLETION_MAX_STDOUT_BYTES: int = 1_048_576

    DATABASE_URL: str = "sqlite:///./pm_platform.db"

    # Personal Signal Intelligence remains opt-in until its fixture-only slices
    # have passed release review.  New writes are independently guarded so a
    # mode change alone can never begin intake.
    INTELLIGENCE_MODE: str = "legacy"
    INSIGHT_WRITES_ENABLED: bool = False
    DECISION_PIPELINE_V2_ENABLED: bool = False
    INSIGHT_MIGRATION_ACTIVATION_ENABLED: bool = False
    INSIGHT_PROJECTION_ENABLED: bool = False
    # A missing rate revision or allowance denies every paid operation.  Fixture
    # tests configure these explicitly; production defaults never spend.
    INTELLIGENCE_RATE_REVISION: str = ""
    INTELLIGENCE_SENSING_ALLOWANCE_MICROS: int | None = None
    INTELLIGENCE_DECISION_ALLOWANCE_MICROS: int | None = None
    # The total allowance and a single-call ceiling are deliberately separate:
    # reservations consume the former, while each verdict is bounded by the
    # latter.  Leave either unset to deny the operation rather than spend.
    INTELLIGENCE_KNOWLEDGE_ALLOWANCE_MICROS: int | None = None
    INTELLIGENCE_KNOWLEDGE_MAXIMUM_MICROS: int | None = None
    KNOWLEDGE_RUBRIC_PATH: str | None = None

    # Server-side API authentication. Every endpoint except GET /health requires
    # an `Authorization: Bearer <PM_PLATFORM_API_TOKEN>` header. The same token is
    # provisioned in the calling client's environment. If left empty, the server fails
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
    # an external operations service has taken over auto-triage archive ownership.
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

    # Optional: send review links to a separate review UI instead of this
    # service's own /runs/{id}/review page. Set it to that UI's base URL and
    # links become "<REVIEW_UI_BASE_URL>/runs/<run_id>"; leave it empty and
    # nothing changes.
    #
    # Why this exists: the built-in review page is behind the same bearer auth
    # as every other route (app.api.deps.require_auth reads the Authorization
    # header and nothing else), so a link to it is a 401 in a browser — the
    # place these links are actually opened. A deployment that runs a separate,
    # browser-reachable console can point at it here rather than fork the
    # engine.
    REVIEW_UI_BASE_URL: str = ""

    # Notifications — Gate 1/2/3 alerts.
    # Leave a field empty ("") to disable that provider.
    # Both providers can be active simultaneously.
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    SLACK_WEBHOOK_URL: str = ""  # Incoming Webhook URL from Slack App settings

    # Master switch for pm-engine's built-in gate push (US-48). Default True
    # (local/dev/test). Set False in a deployment that hands gate + terminal
    # messaging to an external operations service (which polls the queues and composes
    # conversational messages). When False, build_notifier() wires no providers
    # so all send_gate1/2/3 calls are no-ops. Reversible — flip + restart.
    GATE_NOTIFICATIONS_ENABLED: bool = True

    # Path to gate0-state.json owned by an external operations profile. When set, POST
    # /signals rejects any request whose source_ref matches a filename already in
    # the `skipped` bucket — preventing file_watch or other callers from creating
    # a live signal record for a file that PM has explicitly triaged out. Empty
    # string (default) disables the check; the engine is permissive.
    GATE0_STATE_FILE: str = ""

    # Runtime overrides for per-product scoring (S5 weights + routing thresholds),
    # written by an external configuration surface. The git-managed scoring.yaml in context
    # stays the baseline and is never written; this file layers on top of it, so
    # a weight can be retuned from the dashboard without a commit and without the
    # baseline losing its meaning as the declared intent.
    #
    # Shape: {"<product_id>": {"impact": 0.4, "kill_threshold": 1.6, ...}}
    # Empty (default) or absent file = baseline only, i.e. exactly the behaviour
    # before this existed.
    SCORING_OVERRIDES_FILE: str = ""

    # Runtime overrides for engine policy thresholds, written by an external
    # configuration surface and read per call by app/services/runtime_overrides.py.
    # Same rationale as SCORING_OVERRIDES_FILE: settings stay the baseline, this
    # layers deltas, and unset or absent means the configured values apply
    # unchanged.
    #
    # Shape: {"AUTO_TRIAGE_THRESHOLD": 4}
    POLICY_OVERRIDES_FILE: str = ""

    # Runtime override for PRODUCT_FAMILIES (below), same shape and rationale as
    # SCORING_OVERRIDES_FILE. The committed dict is illustrative; an operator's
    # real product ids and families live in a file this points at, outside git.
    #
    # Shape: {"<product_id>": "<family>"}
    PRODUCT_FAMILIES_FILE: str = ""

    @model_validator(mode="after")
    def _resolve_decision_root_aliases(self) -> "Settings":
        if self.DECISION_CONTEXT_ROOT != DEFAULT_DECISION_CONTEXT_ROOT:
            self.DECISION_SYSTEM_ROOT = self.DECISION_CONTEXT_ROOT
            return self
        if self.DECISION_SYSTEM_ROOT:
            self.DECISION_CONTEXT_ROOT = self.DECISION_SYSTEM_ROOT
        self.DECISION_SYSTEM_ROOT = self.DECISION_CONTEXT_ROOT
        return self

    @property
    def decision_system_root(self) -> str:
        """`DECISION_SYSTEM_ROOT`, narrowed to `str`.

        The field itself is typed `str | None` because it also accepts the
        legacy env var name as raw input, but `_resolve_decision_root_aliases`
        always resolves it to `DECISION_CONTEXT_ROOT` (a plain `str`) by the
        time validation completes — this is that guarantee, typed, so callers
        don't each need their own `str | None` check.
        """
        assert self.DECISION_SYSTEM_ROOT is not None
        return self.DECISION_SYSTEM_ROOT


settings = Settings()


# Product families (US-49 conservative fan-out). A routing/grouping layer only —
# NOT the workflow unit: per-product decision-context, the eval regression fixtures, and the
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
    """The product's family, or "unassigned" for products not yet mapped.

    Checks PRODUCT_FAMILIES_FILE first, then the illustrative dict above — so a
    deployment with its own real product ids overrides the committed examples
    without editing this file. Absent, unreadable, or malformed file means the
    dict above is the whole answer, same as before this existed.
    """
    import json
    import pathlib

    if settings.PRODUCT_FAMILIES_FILE:
        try:
            data = json.loads(pathlib.Path(settings.PRODUCT_FAMILIES_FILE).read_text())
            if isinstance(data, dict) and product_id in data:
                return str(data[product_id])
        except Exception:  # noqa: BLE001 — a bad override must never stop the pipeline
            pass
    return PRODUCT_FAMILIES.get(product_id, "unassigned")


def review_url_for(run_id: str) -> str:
    """The link a human opens to review one run.

    Built in one place because it had been built in two, and the pair is easy
    to change by half: the path differs between the built-in page
    (``/runs/{id}/review``) and an external review UI (``/runs/{id}``), so a
    caller that hardcodes one shape silently emits a broken link under the
    other setting.

    Returns "" when no base is configured, which the callers treat as "emit no
    review_url" rather than a relative link.
    """
    base = (settings.REVIEW_UI_BASE_URL or "").rstrip("/")
    if base:
        return f"{base}/runs/{run_id}"
    base = (settings.BASE_URL or "").rstrip("/")
    return f"{base}/runs/{run_id}/review" if base else ""
