# S2K bounded inference route — release packet

**Status:** Engine implementation is merged in PR #153 and the matching Hermes implementation is merged in PR #1256. On Mac Studio, Engine and Hermes main are at merged descendants, and the Engine bridge is configured to use the provisioned `[Agent] researcher` profile. The previous one-shot remains held with an unknown reservation; no triage output was persisted. Hermes PR #1256 CI selected system Python 3.9.6 instead of provisioned Python 3.13, so PyYAML installed only for Python 3.9 and Pillow 12.3 was filtered out. A deterministic interpreter-selection fix is prepared locally; CI rerun is pending. Recurring intake remains inactive.

## Scope and route contract

S2K semantic triage and the explicitly scoped Insight jobs use a bounded completion subprocess. Its only permitted route order is OpenAI Codex OAuth, Anthropic OAuth, then OpenAI API. The bridge uses Hermes' provider-specific client resolver and direct provider adapters; it does not call Hermes' generic `call_llm()` or `async_call_llm()` fallback ladder. The OpenAI API route resolves through Hermes' internal `openai-api` provider so it cannot inherit the `openai` custom endpoint alias.

The Engine subprocess receives only an allowlisted environment and sends the request on stdin. It validates the route identity and response schema, bounds stdout/stderr, kills and reaps timed-out children, and emits only route and usage-status telemetry. The bridge's 60-second completion deadline is shared across the remaining providers, with each adapter call cancellation-bounded to its share. `usage: null` remains unknown; an all-zero usage object is rejected. Product Decision and generic Insight triage continue to use `engine.llm`.

## Merged implementation and verification

The Engine implementation is merged at `cdac35968d7b49ce65951b79af86233be3612968` (PR #153). Mac Studio's Hermes main checkout is at `456274a441ceffe2ecc743a408303cf27130e43f`, which contains PR #1256. The running Engine launch agent serves `/health` with HTTP 200. Engine `.env` points `S2K_COMPLETION_COMMAND` at the bridge in that Hermes checkout and `S2K_COMPLETION_PROFILE_HOME` at `/Users/runtime/.hermes/profiles/researcher`. These facts verify the deployed source and route configuration, not a successful provider route.

Verified locally with fixtures only:

- Control-plane bounded route and stdin/stdout protocol suite: 25 passed; isolated S2K bootstrap/profile test: 1 passed. Coverage includes the exact three-route ordering, missing credentials, retryable and non-retryable errors, endpoint/adapter identity, malformed input/output, bounded stdin, and sanitized one-object protocol output.
- Engine full suite via `DECISION_CONTEXT_ROOT=/Users/jackpark/workspace/ai-assets/jackhpark-pm-decision-context make test`: 803 passed (418 unit, 32 decision, 146 insights, 207 integration).
- Engine focused route/provider tests cover timeout/backpressure cleanup, replay, unknown usage, and route isolation. `make lint` passed Ruff and mypy across 87 source files; `git diff --check` passed.
- Engine subprocess fixtures cover timeout, oversized stdout/stderr, malformed responses, nonzero exit, unknown usage, fabricated zero usage, environment isolation, caller-model override rejection, and Product Decision provider independence.
- Final review findings were covered by regressions and fixes: Anthropic API-key clients are rejected from the OAuth lane; Hermes-native Anthropic transport errors use only the next approved route; route dispatch has an enforced attempt deadline; subprocess readers and stdin are monitored together and children are killed/reaped on output overflow; replay is resolved before provider construction; transport ambiguity keeps its unknown reservation and rejects duplicate model execution.

The fixture implementation made no provider request. The Mac Studio researcher profile has the approved Sol mapping: `gpt-6-sol` for OpenAI Codex OAuth, `claude-sonnet-5` for Anthropic OAuth, and `gpt-6-sol` for OpenAI API fallback. Its credential pool has entries for `openai-codex`, `anthropic`, and `openai-api`, and its `.env` contains non-empty `OPENAI_API_KEY` and `CLAUDE_CODE_OAUTH_TOKEN` entries. Values were not displayed. This confirms configuration and credential artifacts only; no live provider validity check was run. The archived `s2k` profile is not used or to be recreated. `auxiliary.s2k` is the route namespace inside the `researcher` profile, not a separate Hermes profile.

After changing CI to select Python 3.13 explicitly, the Hermes worktree passed `make test` (6496 passed, 28 skipped) and `make docs-check` (875 passed, 15 skipped). GitHub CI has not rerun because the fix has not been published in a PR.

The previous one-shot is not a successful route proof. A read-only query on Mac Studio found reservation `070aa085-b46b-4581-be58-7fd3b185085e` for operation `435e36a4-609a-5deb-bf4d-2280a6940d6a`, allowance class `sensing`, maximum `$0.10`, state `unknown`; its `intelligence_triage` row remains `running` with an empty payload. `GET /runs/d91e60bf-0056-4a21-83ae-a2893d0fa9d0` returns 404. `GET /insight-operations` is aggregate-only and reports `$1.20` in unknown reservations, so it cannot identify this reservation. The exact operation remains unreconciled. Preserve it and do not retry. An authenticated read-only `GET /insight-operations/{operation_id}` is implemented locally; it returns triage state and an allowlisted reservation projection, never the stored result payload or provider metadata. It must be reviewed, merged, and deployed before using it against the Mac Studio record.

## Remaining cutover gates

- Reconcile the held run and unknown reservation using the new read-only operation endpoint after it is merged and deployed, or use an owner-led reconciliation path. Do not clear or retry the reservation until terminal state and duplicate-execution safety are established.
- Profile identity/path is confirmed on Mac Studio: only `researcher` is provisioned in the active Hermes profile root, Engine points to it, and `s2k` is absent from the inspected profile roots. The old `s2k` profile was intentionally archived; no restore, migration, or deletion is part of this cutover.
- Route configuration and credential artifact presence are confirmed without exposing values. Do not run a standalone provider smoke test; verify route identity only in a separately approved end-to-end one-shot.
- Fix CI's interpreter selection: the current workflow invoked system Python 3.9.6 despite provisioning Python 3.13.15. The local change uses the 3.13 executable explicitly for install/preflight/pytest and supplies its bin directory to `make docs-check`. Rerun CI after the change is authorized into a PR.
- Mac Studio already runs the merged Engine and Hermes source, the service is healthy, and its bridge command/profile settings are configured. Keep intake paused and the allowance at zero; no restart or deployment is currently required to establish those observed facts.
- The prior one-shot authorization is consumed. Obtain separate approval for the next paid one-shot, capped at `$5`, only after the prior reservation is reconciled. Record attempted/successful provider and model, measured usage or `unknown`, cost or `unknown`, terminal reservation state, and persisted artifacts.
- Review the end-to-end result, provenance, replay behavior, route isolation, and rollback before making a separate recurring-activation decision. Sol remains the baseline; Luna requires separately approved representative-task evaluation.
- If recurring activation is approved, enable the bounded schedule and allowance separately, inspect the first scheduled run, and retain a rollback to paused intake and zero allowance.

No provider smoke test, paid retry, deployment, restart, allowance change, or recurring activation is authorized by this release packet update.

## Rollback

Keep intake paused and the sensing allowance at zero until cutover is explicitly authorized. The Engine factory fails closed when `S2K_COMPLETION_COMMAND` or `S2K_COMPLETION_PROFILE_HOME` is unset. If a reviewed release needs to be withdrawn, pause intake, restore the allowance to zero, revert only the S2K-specific route configuration, and verify the generic `engine.llm` path remains in use. Do not remove a runtime profile until its consumers and persistent state have been inventoried.
