# Notification & Delivery Contract
## pm-engine ↔ the operations plane

**Status:** Normative. Engine side effective as of US-48 cutover (verified in
production 2026-07-02); ops-plane-side channel policy items tracked separately.

---

## Why this document exists

By 2026-07-02 the notification surface had drifted into three uncoordinated
pieces, observed live in production:

- Gate prompts (Gate 1/2) delivered by the ops plane to **Discord**;
- reconciler digests (signal auto-expiry) delivered by the ops plane to **Telegram**;
- pm-engine's built-in `FanoutNotifier` disabled in production
  (`GATE_NOTIFICATIONS_ENABLED=false`) but fully wired and one flag-flip away
  from re-creating a **second, duplicate** delivery path — while
  `ARCHITECTURE.md` still claimed the engine was the notification owner.

No single place said who sends what, where, and how duplicates are prevented.
This document is that place. **If delivery behavior and this document disagree,
one of them is wrong — fix whichever it is, in the same change.**

---

## 1. Ownership (normative)

| Concern | Owner | Notes |
|---|---|---|
| Gate state machine + queue queries | **pm-engine** | `waiting_direction` / `waiting_approval` / `waiting_routing_review`, terminal statuses, event filters |
| Review payloads | **pm-engine** | `gate1_review` / `gate3_review` on `GET /runs/{id}`; artifacts API |
| Browser review page | **pm-engine** | `GET /runs/{id}/review` (Tailscale-reachable) |
| Message **composition** | **Ops plane** | Conversational, LLM-authored; engine templates are not used in production |
| Message **delivery** (channel choice, sending) | **Ops plane** | Sole production delivery owner |
| Deduplication | **Ops plane** | Keyed as defined in §3 |
| Digest batching & cadence | **Ops plane** | e.g. auto-triage digest, expiry sweeps |

**pm-engine composes and delivers NO human-facing messages in production.**
The built-in `FanoutNotifier` (Telegram/Slack templates in
`app/services/notifier.py`) is a **local/dev fallback only**, gated by
`GATE_NOTIFICATIONS_ENABLED` (default `true` for local/test, **`false` on the
iMac**). Enabling it in production is a contract violation: every gate
transition would be announced twice by two different systems.

---

## 2. What the engine exposes (the interface the ops plane consumes)

| Purpose | Interface | Notes |
|---|---|---|
| Gate 1 queue (depth decision) | `GET /runs?status=waiting_direction` | Enrich from `gate1_review` (S2 insight, suggested depth, pillars) |
| Gate 2 queue (evaluation approval) | `GET /runs?status=waiting_approval` | Persona scores via `gate3_review.personas` precursor (`GET /runs/{id}?include_outputs` or artifacts); include review page link `{BASE_URL}/runs/{id}/review` |
| Gate 3 queue (routing review) | `GET /runs?status=waiting_routing_review` | Enrich from `gate3_review` (routing, composite, assumptions, closing window) |
| Terminal results | `GET /runs?status=completed\|killed&since=…` | Result messages for note/structure/evaluate/decide completions — the "evaluate-run silence" fix folded into US-48 |
| Decision-event digests | `GET /runs?event=auto_triaged\|deepen\|direction\|…&since=…` | Every human/system gate decision is a persisted `approval_event` |
| Failure alerts | `GET /runs?status=failed` + `failed_stage`/`error` fields | `retry_exhausted` is also emitted as a structured log event |
| Batch/synthesis results | `GET /runs/batch/{batch_id}` | `synthesis` non-null once all siblings settle |
| Gate 2 review link | `review_url` on every run object | Built by the engine from `BASE_URL` (Tailscale address in prod) so the delivery owner never needs the engine's network config |

**Re-notification / dedup key:** `(run_id, status, updated_at)`.
`updated_at` bumps on every run change, so a run re-entering the same gate
(e.g. `waiting_approval` again after a Gate 2 revise) is a **new** notification;
the same tuple observed twice is a duplicate and must not be re-sent. This is
the documented reason `updated_at` exists on the run row.

> **Implementation status (verified 2026-07-02):** the ops plane's gate-watcher already
> implements this exactly — `state/gate-notified.json` keyed `<run_id>:<status>`
> with `run_updated_at` comparison for gates, notify-once for terminal states,
> a 24h staleness re-nag for un-actioned gates, and a suppression rule for
> PM-chosen archive completions. Terminal-result messages for non-decide
> completions (the "evaluate-run silence") are likewise already implemented.
> See the ops plane's own gate-watcher skill definition.

---

## 3. Channel policy (normative for the ops plane; recorded from the verified implementation)

Messages fall into three classes. Classes A and B follow **origin-affinity
routing**; class C goes to the default channel. Changing this policy is allowed —
but the change includes updating this table, in the same change.

| Class | Definition | Routing (verified 2026-07-02) | Rules |
|---|---|---|---|
| **A — Decision-required prompts** | Gate 1 / Gate 2 / Gate 3: the run is blocked on a PM decision | **Origin-affinity**: delivered to the platform+channel the run's conversation started on (`state/run-chat-map.json`); runs with no recorded origin (auto-sensing, API-started) fall back to `GATE_NOTIFY_DEFAULT_CHAT_ID` (Telegram shared group) | One message per dedup tuple (+ 24h staleness re-nag); states the pending decision and options in plain language, no raw API text; Gate 2 carries the `review_url` link |
| **B — Result messages** | Terminal-run outcomes (completed / killed / failed), batched per signal | Same origin-affinity routing as class A — the result lands where the decision conversation happened | Notify-once, never re-announced; PM-chosen-archive completions suppressed; failed = system-fault alert, not an outcome |
| **C — Ops digests & alerts** | Signal expiry/reconciler sweeps, gate0 intake digests, engine watchdog | Telegram default channel | Batched where possible; a digest never demands a decision |

Rationale: a decision prompt should arrive **where the PM was already talking
about that run** (origin-affinity), so the conversation and the decision stay in
one thread; everything that is not tied to a live conversation goes to the one
default channel, which can be muted without missing a gate.

---

## 4. Violations (what "wrong" looks like)

- pm-engine composing or sending production messages (flag flipped without
  removing the ops-plane path first).
- Two systems announcing the same transition (the dedup tuple in §2 exists to
  make this detectable).
- A message class split across channels, or a channel change made without
  updating §3.
- A new engine-side state or event that the ops plane cannot observe via §2 — extend the
  interface table in the same PR that adds the state.

---

## 5. Item status (US-50; audited against the live gate-watcher 2026-07-02)

- [x] Channel policy recorded — §3 documents the verified origin-affinity
      routing (`run-chat-map.json` + `GATE_NOTIFY_DEFAULT_CHAT_ID` fallback).
- [x] Dedup — already implemented in the gate-watcher (`gate-notified.json`,
      key `<run_id>:<status>` + `run_updated_at`, staleness re-nag, terminal
      notify-once). Matches §2 exactly.
- [x] Terminal-result messages for non-decide completions — already implemented
      (with per-signal batching and PM-chosen-archive suppression).
- [x] Gate 2 review link — engine exposes `review_url` on the run object; the
      gate-watcher skill instructs including it in Gate 2 messages.
- [x] `deepen` relay — the gate-watcher terminal message offers deepening for
      completed note/structure/evaluate runs; SOUL.md / control-plane skill
      carry the `POST /runs/{id}/deepen` relay rule.

The ops plane's runtime skill/config files live outside this repo, on the host
that runs it; this contract is the engine-side
half of the boundary and the single place the policy is recorded.
