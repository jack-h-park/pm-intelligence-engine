# S2K bounded inference route — release packet

**Status:** Fixture-only implementation is complete in local worktrees. Release is on hold; no profile or route is installed or activated.

## Scope and route contract

S2K semantic triage and the explicitly scoped Insight jobs use a bounded completion subprocess. Its only permitted route order is OpenAI Codex OAuth, Anthropic OAuth, then OpenAI API. The bridge uses Hermes' provider-specific client resolver and direct provider adapters; it does not call Hermes' generic `call_llm()` or `async_call_llm()` fallback ladder. The OpenAI API route resolves through Hermes' internal `openai-api` provider so it cannot inherit the `openai` custom endpoint alias.

The Engine subprocess receives only an allowlisted environment and sends the request on stdin. It validates the route identity and response schema, bounds stdout/stderr, kills and reaps timed-out children, and emits only route and usage-status telemetry. The bridge's 60-second completion deadline is shared across the remaining providers, with each adapter call cancellation-bounded to its share. `usage: null` remains unknown; an all-zero usage object is rejected. Product Decision and generic Insight triage continue to use `engine.llm`.

## Local implementation and verification

The control-plane implementation is in `/Users/jackpark/.codex/worktrees/7bc3/jackhpark-hermes-control-plane`, based on `752cff995e22abf66f33b005b384d9f13f5511e3`. The Engine implementation is in `/Users/jackpark/.codex/worktrees/s2k-bounded-inference-engine`, based on `8545b7c3427aae96722e7982dcad1c6a4944b643`. Both implementations are uncommitted; these are baseline SHAs, not implementation commit SHAs.

Verified locally with fixtures only:

- Control-plane bounded route and stdin/stdout protocol suite: 25 passed; isolated S2K bootstrap/profile test: 1 passed. Coverage includes the exact three-route ordering, missing credentials, retryable and non-retryable errors, endpoint/adapter identity, malformed input/output, bounded stdin, and sanitized one-object protocol output.
- Engine full suite via `DECISION_CONTEXT_ROOT=/Users/jackpark/workspace/ai-assets/jackhpark-pm-decision-context make test`: 803 passed (418 unit, 32 decision, 146 insights, 207 integration).
- Engine focused route/provider tests cover timeout/backpressure cleanup, replay, unknown usage, and route isolation. `make lint` passed Ruff and mypy across 87 source files; `git diff --check` passed.
- Engine subprocess fixtures cover timeout, oversized stdout/stderr, malformed responses, nonzero exit, unknown usage, fabricated zero usage, environment isolation, caller-model override rejection, and Product Decision provider independence.
- Final review findings were covered by regressions and fixes: Anthropic API-key clients are rejected from the OAuth lane; Hermes-native Anthropic transport errors use only the next approved route; route dispatch has an enforced attempt deadline; subprocess readers and stdin are monitored together and children are killed/reaped on output overflow; replay is resolved before provider construction; transport ambiguity keeps its unknown reservation and rejects duplicate model execution.

No live provider request was made. No live credential or API key was read. The S2K profile template deliberately leaves all model IDs unset, so the route rejects configuration before provider resolution until model IDs are separately selected and approved.

## Release gates

The following are separate decisions and have not been authorized by the implementation approval:

- Choose and approve model IDs for all three provider lanes.
- Install the S2K profile on the runtime host and configure provider credentials.
- Approve a paid/live canary, its usage allowance, and whether API fallback may incur cost.
- Configure Engine subprocess settings and deploy/restart the Engine.
- Activate any recurring intake or schedule.

## Rollback

No production state has changed, so there is no live rollback to perform. Before activation, leave `S2K_COMPLETION_COMMAND` and `S2K_COMPLETION_PROFILE_HOME` empty; the Engine factory fails closed when they are unset. If a later reviewed release needs to be withdrawn, revert the S2K-specific routing and bridge changes in both repositories, remove only the S2K profile and its dedicated environment configuration from the host, and verify the generic `engine.llm` path remains in use. Do not remove unrelated profile data or provider configuration.
