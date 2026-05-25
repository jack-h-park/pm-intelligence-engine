# Integration Principles
## jackhpark-pm-agentic-platform × Hermes Operations Plane

**Version:** 1.0  
**Last updated:** 2026-05-24

This document defines the non-negotiable boundaries between pm-platform
(the workflow execution engine) and any external consumer, in particular
the Hermes operations plane.

---

## Core Principle

> **pm-platform is workflow law. Hermes is workflow operator.**

pm-platform owns the state machine, data model, stage execution, and gate
semantics. External systems interact with pm-platform exclusively through
its HTTP API. pm-platform does not negotiate its internal state on behalf
of an external caller.

---

## Principle 1 — No Direct Database Mutation

External systems (Hermes or any other consumer) **must not** read from or
write to the SQLite (or future PostgreSQL) database directly.

**Allowed:** `GET /runs`, `GET /runs/{id}`, `POST /signals`, gate endpoints.

**Forbidden:** `sqlite3 pm_platform.db "UPDATE workflow_runs SET status='completed'"`.

**Why:** Direct mutation bypasses the state machine, skips `completed_at` stamping,
skips event emission, and breaks Hermes's ability to reliably observe transitions.

---

## Principle 2 — No Gate Semantic Override

Hermes may bridge a PM response to a gate API call. It may not invent new
gate states, reinterpret what `approve`/`revise`/`reject` mean, or create
approval records through any path other than the official endpoints.

**Allowed:** `POST /runs/{id}/approve` (bridging a PM's Telegram reply).

**Forbidden:** Custom approval states, file-based approval hacks, setting
`status='approved'` directly (this status does not exist in the state machine).

**Why:** Gate semantics are contracts. If Hermes changes what they mean,
runs produced by pm-platform become unreliable for audit and re-processing.

---

## Principle 3 — No Wiki Write Ownership in pm-platform

pm-platform does not write to `WIKI_ROOT` as part of run completion.
`wiki_sync.py` exists as a utility adapter and documents canonical paths;
it is not called from any completion path.

**Allowed (Hermes):** Poll for `completed`/`killed` events via `GET /runs`,
read artifact via `GET /runs/{id}?include_outputs=true`, write to wiki.

**Forbidden (pm-platform completion paths):** Direct calls to
`sync_executive_summary()` or any WIKI_ROOT write inside `run_finalizer`,
`approvals.py`, `routing_review.py`, or `runs.py` completion branches.

**Why:** If pm-platform and Hermes both write to the wiki, conflicts arise
and idempotency guarantees are unclear. One owner writes; the other reads.

---

## Principle 4 — External Repos Through Explicit Contracts Only

pm-platform reads from `DECISION_SYSTEM_ROOT` (prompts, context) and writes
to it (run exports). No other external filesystem interaction is permitted
from completion paths.

Any new external-repo interaction must be documented in:
- `docs/EXPORT_AND_SYNC_CONTRACT.md` — for file write contracts
- `docs/API_CONTRACT.md` — for API surface consumed by Hermes

**Forbidden:** Ad-hoc file reads/writes to external repos without documented
contracts; environment-conditional logic that writes to different repos based
on undocumented flags.

---

## Principle 5 — No `current_stage` Direct Mutation from External Callers

`current_stage` is an internal progress indicator set exclusively by stage
execution functions and `run_finalizer`. External callers must not pass
`current_stage` in any API payload or expect it to be a controllable field.

---

## What Hermes May Do

| Action | How |
|--------|-----|
| Submit new signals | `POST /signals` |
| Poll for actionable runs | `GET /runs?status=awaiting_direction` |
| Bridge PM direction | `POST /runs/{id}/direction` |
| Poll for Gate 2 runs | `GET /runs?status=waiting_approval` |
| Bridge PM approval | `POST /runs/{id}/approve\|revise\|reject` |
| Poll for Gate 3 runs | `GET /runs?status=waiting_routing_review` |
| Bridge PM routing decision | `POST /runs/{id}/routing-review` |
| Read completed run artifacts | `GET /runs/{id}?include_outputs=true` |
| Write to wiki after completion | Direct WIKI_ROOT write (Hermes-owned) |
| Send notifications | Hermes-owned (not pm-platform) |
| Schedule harvesting | Hermes-owned cron/jobs |

---

## What Hermes Must NOT Do

| Forbidden action | Why |
|-----------------|-----|
| Direct SQLite/DB write | Bypasses state machine |
| Invent approval states | Gate semantics are a contract |
| Write to WIKI_ROOT from pm-platform code | Ownership conflict |
| Call internal stage functions directly | Violates stage contract |
| Modify `current_stage` externally | Internal progress marker only |
| Create file-based approvals | Bypasses audit trail |
