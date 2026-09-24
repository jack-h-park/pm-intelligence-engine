# Researcher S2K Route — Local Release Packet

**Status:** Fixture-only integration packet for the merged Researcher identity and S2K route. Not installed, activated, or deployed.

## Purpose

The Hermes profile ID is `researcher` and its approved character alias is Nora, defined in
the Hermes control-plane distribution. The PM
Engine owns questions, request schemas, triage decisions, evidence bundles, Insight
persistence, and knowledge-verdict semantics. The bounded Hermes bridge supplies Nora's
identity context only to S2K completion calls.

## Route ownership

- `/insight-triage/interest` resolves the Engine-owned interest first, then uses the bounded
  S2K provider for semantic triage.
- Explicitly named scoped Candidate and backfill worker entrypoints use the bounded S2K
  provider.
- Generic `/insight-triage`, generic Insight queue workers, portfolio triage, workflow
  stages, and Product Decision continue using their existing providers.
- The Engine sends the S2K bridge a strict JSON request on stdin. The bridge reads
  `IDENTITY.md` from `HERMES_HOME`. It combines that identity with any Engine system
  instructions into one system message because Hermes' primary Codex adapter keeps only
  one system instruction; all non-system Engine messages retain their original order and
  content.
- The Engine child process receives only a minimal environment plus the configured
  `HERMES_HOME`. API keys and the global Hermes configuration do not cross this boundary.
- Engine subprocess input, stdout, and stderr are bounded; timeout and output overflow kill,
  close the pipes, and reap the child. The Anthropic OAuth attempt applies its deadline to
  the native SDK client, an absolute stream deadline in the worker thread, and a socket-shutdown
  watchdog for SSE keepalives the SDK filters before Hermes' event hook.
- Missing or unusable provider usage is `null`. The Engine rejects fabricated all-zero usage
  instead of recording it as measured.

## Required settings

```dotenv
S2K_COMPLETION_COMMAND=
S2K_COMPLETION_PROFILE_HOME=
S2K_COMPLETION_TIMEOUT_SECONDS=60
S2K_COMPLETION_MAX_STDOUT_BYTES=1048576
```

The command must be a static argv with an absolute executable path. The profile home must
be the isolated researcher runtime directory. Missing settings fail closed. The completion
bridge enforces the provider allowlist and endpoint identity; no live provider call is part
of fixture validation.

## Local verification

- Control-plane focused bridge/profile/identity suite: 65 passed, including the progressing
  Codex worker-deadline regression.
- Control-plane `make docs-check`: 847 passed, 25 skipped.
- Control-plane full `make test` ran twice with xdist: each run had 6,395 passed and 34
  skipped, plus one failure in a different unrelated test. Both failed tests passed when
  rerun individually; the parallel-suite failures remain recorded as flaky verification.
- Engine `make test`: 779 passed across unit, decision, Insight, and integration suites
  (398 + 32 + 142 + 207). `make lint` passed Ruff and mypy across 87 source files.
- Both worktrees passed `git diff --check`.
- Reviewer's real Codex adapter/OpenAI SDK loopback with 20 ms SSE text deltas: the 0.15 s
  attempt deadline raised at 0.153 s and `asyncio.run` returned at 0.154 s.
- Reviewer's real Anthropic SDK/Hermes adapter loopback: ping-only and content SSE streams
  terminated in 0.15–0.20 seconds with a 0.15-second route deadline.

## Release boundary

This packet records local routing and fixture contracts only. The initial Sol tier is
approved as a plan decision with the mapping `gpt-6-sol` → `claude-sonnet-5` → `gpt-6-sol`;
the checked-in fixture template remains blank. A Luna step-down requires a separately
authorized side-by-side quality evaluation on representative triage and Insight synthesis
tasks. This packet does not authorize profile installation, provider credentials, live or
paid evaluation calls, intake scheduling, source-scope expansion, Engine restart/deployment,
notifications, Product Decision handoff, or visual asset work. Each remains separately
gated.
