# Notification & Delivery Contract
## pm-engine ↔ Hermes-ops (Iris)

**Status:** Normative. Engine side effective as of US-48 cutover (verified in
production 2026-07-02); Iris-side channel policy items tracked in US-50.

---

## Why this document exists

By 2026-07-02 the notification surface had drifted into three uncoordinated
pieces, observed live in production:

- Gate prompts (Gate 1/2) delivered by Iris to **Discord**;
- reconciler digests (signal auto-expiry) delivered by Iris to **Telegram**;
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
| Message **composition** | **Hermes-ops (Iris)** | Conversational, LLM-authored; engine templates are not used in production |
| Message **delivery** (channel choice, sending) | **Hermes-ops (Iris)** | Sole production delivery owner |
| Deduplication | **Hermes-ops (Iris)** | Keyed as defined in §3 |
| Digest batching & cadence | **Hermes-ops (Iris)** | e.g. auto-triage digest, expiry sweeps |

**pm-engine composes and delivers NO human-facing messages in production.**
The built-in `FanoutNotifier` (Telegram/Slack templates in
`app/services/notifier.py`) is a **local/dev fallback only**, gated by
`GATE_NOTIFICATIONS_ENABLED` (default `true` for local/test, **`false` on the
iMac**). Enabling it in production is a contract violation: every gate
transition would be announced twice by two different systems.

---

## 2. What the engine exposes (the interface Iris consumes)

| Purpose | Interface | Notes |
|---|---|---|
| Gate 1 queue (depth decision) | `GET /runs?status=waiting_direction` | Enrich from `gate1_review` (S2 insight, suggested depth, pillars) |
| Gate 2 queue (evaluation approval) | `GET /runs?status=waiting_approval` | Persona scores via `gate3_review.personas` precursor (`GET /runs/{id}?include_outputs` or artifacts); include review page link `{BASE_URL}/runs/{id}/review` |
| Gate 3 queue (routing review) | `GET /runs?status=waiting_routing_review` | Enrich from `gate3_review` (routing, composite, assumptions, closing window) |
| Terminal results | `GET /runs?status=completed\|killed&since=…` | Result messages for note/structure/evaluate/decide completions — the "evaluate-run silence" fix folded into US-48 |
| Decision-event digests | `GET /runs?event=auto_triaged\|deepen\|direction\|…&since=…` | Every human/system gate decision is a persisted `approval_event` |
| Failure alerts | `GET /runs?status=failed` + `failed_stage`/`error` fields | `retry_exhausted` is also emitted as a structured log event |
| Batch/synthesis results | `GET /runs/batch/{batch_id}` | `synthesis` non-null once all siblings settle |

**Re-notification / dedup key:** `(run_id, status, updated_at)`.
`updated_at` bumps on every run change, so a run re-entering the same gate
(e.g. `waiting_approval` again after a Gate 2 revise) is a **new** notification;
the same tuple observed twice is a duplicate and must not be re-sent. This is
the documented reason `updated_at` exists on the run row.

---

## 3. Channel policy (normative for Iris)

Messages fall into three classes. **Each class has exactly one channel.**
Moving a class to a different channel is allowed — but the move includes
updating this table, in the same change.

| Class | Definition | Channel (as of 2026-07-02) | Rules |
|---|---|---|---|
| **A — Decision-required prompts** | Gate 1 / Gate 2 / Gate 3: the run is blocked on a PM decision | Discord (ops channel) | One message per dedup tuple; must state the pending decision, the options, and the next action; must carry the review link for Gate 2 |
| **B — Informational digests** | Terminal-run results, auto-triage digest, signal expiry/reconciler sweeps | Telegram | Batched; no per-item pings; a digest never demands a decision |
| **C — Ops alerts** | Run failures, retry exhaustion, service health | Telegram (until an ops channel is designated) | Immediate; include `failed_stage`/`error` so no log-grepping is needed |

Rationale: the PM should be able to answer "do I owe the system a decision?"
by checking exactly one place, and mute everything else without missing a gate.

---

## 4. Violations (what "wrong" looks like)

- pm-engine composing or sending production messages (flag flipped without
  removing the Iris path first).
- Two systems announcing the same transition (the dedup tuple in §2 exists to
  make this detectable).
- A message class split across channels, or a channel change made without
  updating §3.
- A new engine-side state or event that Iris cannot observe via §2 — extend the
  interface table in the same PR that adds the state.

---

## 5. Open items (Iris-side, tracked as US-50)

- [ ] Confirm/record the intended Class A channel (Discord assumed from
      observed behavior) and consolidate Class B/C to the table above.
- [ ] Implement dedup keyed on `(run_id, status, updated_at)` if not already.
- [ ] Terminal-result messages for non-decide completions (note/structure/
      evaluate) per US-48's folded scope.
- [ ] Include the Gate 2 review-page link in Class A messages (engine exposes
      `BASE_URL`-based link; Iris currently offers approve/revise/reject inline).

Iris implementation lives in the hermes control-plane repo; this contract is
the engine-side half of the boundary.
