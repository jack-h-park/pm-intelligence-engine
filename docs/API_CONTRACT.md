# API Contract
## pm-intelligence-engine — Public Interface for Hermes

**Version:** 1.1  
**Last updated:** 2026-06-05

## Authentication

Every endpoint **except `GET /health`** requires a bearer token:

```
Authorization: Bearer ${PM_PLATFORM_API_TOKEN}
```

- The server validates the header against `PM_PLATFORM_API_TOKEN` from its own
  environment (launchd / `.env`). The same shared token is provisioned in the
  Hermes client `.env`; all Hermes profiles send it.
- Missing, malformed, or mismatched token → **`401`** (with `WWW-Authenticate: Bearer`).
- If the server is started without `PM_PLATFORM_API_TOKEN` set, it **fails closed**:
  authenticated routes return **`503`** rather than serving an open API. `GET /health`
  stays available so liveness probes / launchd `KeepAlive` do not thrash.
- `GET /health` is intentionally unauthenticated (liveness probe only; returns no data).

**Network bind:** the service binds to **`127.0.0.1:8000`** (loopback only). It is not
reachable off-device. Same-host callers (Hermes on the iMac, local scripts) use
`http://localhost:8000`. Off-device review links must go through HTTPS / a reverse
proxy — not a wide plain-HTTP bind.

**URL policy:**

| Consumer | URL to use |
|----------|-----------|
| Hermes / same-host automation / local scripts | `http://localhost:8000` |
| Off-device review links (iPhone, MacBook) | `BASE_URL` from `.env` (Tailscale IP or HTTPS) |

`BASE_URL` in `.env` is the **public review-link base** only — it appears in Gate 2 notification
links for off-device access. Same-host callers (Hermes on the same iMac, local scripts, health
checks) should use `http://localhost:8000` directly and must not use the Tailscale raw IP HTTP
for same-host calls. Prefer MagicDNS or HTTPS for off-device links when available.

This document defines the canonical public API surface that Hermes (and any
other external consumer) interacts with. The shape of these endpoints is stable.
New endpoints may be added; existing endpoint shapes will not break without a
version bump.

---

## Endpoint Summary

| Method | Path | Purpose | Hermes use |
|--------|------|---------|------------|
| `POST` | `/signals` | Submit a new signal | Harvest submission |
| `GET` | `/signals` | List signals with filters | Inventory check |
| `GET` | `/signals/{id}` | Get a single signal | Detail fetch |
| `POST` | `/runs/start` | Start a pipeline run | Optional (manual start) |
| `GET` | `/runs` | List runs with filters | Polling actionable queues |
| `GET` | `/runs/{id}` | Get run state + outputs | Completion artifact fetch |
| `GET` | `/runs/{id}/artifacts` | List persisted artifacts for a run | Lightweight artifact fetch |
| `POST` | `/runs/{id}/direction` | Gate 1 response | Bridge PM direction |
| `POST` | `/runs/{id}/approve` | Gate 2 approve | Bridge PM approval |
| `POST` | `/runs/{id}/revise` | Gate 2 revise | Bridge PM revision |
| `POST` | `/runs/{id}/reject` | Gate 2 reject | Bridge PM rejection |
| `POST` | `/runs/{id}/routing-review` | Gate 3 confirm/override | Bridge PM routing decision |
| `POST` | `/runs/{id}/reopen` | Revive an auto-triaged run | Auto-triage digest follow-up |
| `GET` | `/runs/{id}/review` | Gate 2 browser review page | Link in Gate 2 notification |
| `GET` | `/health` | Health check (unauthenticated) | Liveness probe |

---

## State Contract

### Gate states (Hermes polls these queues)

| Status | Gate | Action endpoint | Poll query |
|--------|------|----------------|------------|
| `awaiting_direction` | Gate 1 | `POST /runs/{id}/direction` | `GET /runs?status=awaiting_direction` |
| `waiting_approval` | Gate 2 | `POST /runs/{id}/approve\|revise\|reject` | `GET /runs?status=waiting_approval` |
| `waiting_routing_review` | Gate 3 | `POST /runs/{id}/routing-review` | `GET /runs?status=waiting_routing_review` |

### Terminal states

| Status | `completed_at` | Meaning |
|--------|---------------|---------|
| `completed` | ✅ set | Run finished — artifacts available |
| `killed` | ✅ set | Run rejected or kill-confirmed |
| `failed` | ❌ unset | Unexpected error — may need investigation |

### Intermediate states (do not poll these for action)

| Status | Meaning |
|--------|---------|
| `pending` | Created, not yet started |
| `running` | Stage execution in progress |

---

## Endpoint Details

### `POST /signals`
Submit a new signal for processing.

**Request body:**
```json
{
  "product_id": "example-security-product",
  "title": "Android 16 NFC admin control API released",
  "raw_content": "Full article text or summary...",
  "source_url": "https://example.com/article",
  "source_type": "rss"
}
```

`source_type`: `"manual"` | `"rss"` | `"file_watch"`

**Response (201):**
```json
{ "signal_id": "uuid", "product_id": "...", "title": "...", "status": "pending" }
```

**Signal lifecycle:**
- new signals are created with `status="pending"`
- a successful `POST /runs/start` moves the signal to `status="in_run"`
- terminal `completed` and `killed` runs move the signal to `status="done"`
- terminal `failed` runs move the signal back to `status="pending"` so they remain retryable

---

### `GET /runs`
List runs. Hermes uses this for polling actionable queues.

**Query parameters:**
- `product_id` — filter by product
- `status` — filter by status (e.g. `awaiting_direction`, `completed`)
- `routing` — filter by routing (`prd`, `poc`, `kill`)
- `event` — filter by recorded decision event (`auto_triaged`, `reopen`, `approve`,
  `revise`, `reject`, `direction`, `confirm`, `override`). Every human gate decision is
  now persisted (US-44): Gate 1 mode choice (`direction`), Gate 2 (`approve`/`revise`/
  `reject`), Gate 3 routing (`confirm`/`override`) — each records the system suggestion
  vs the PM's choice in `feedback_text`, forming the labeled human-decision dataset.
  `event=auto_triaged` is the canonical query for the Hermes auto-triage digest (US-31).
- `since` — ISO 8601 timestamp; only runs created at or after this time
- `limit` — max results (default 50)

**Response (200):** Array of run objects (see run schema below).

---

### `GET /runs/{id}?include_outputs=true`
Get a single run. Pass `include_outputs=true` to include all stage outputs
(S1–S7 JSON blobs). Hermes uses this to read artifacts before wiki sync.

**Run schema:**
```json
{
  "run_id": "uuid",
  "product_id": "example-security-product",
  "signal_id": "uuid",
  "status": "completed",
  "current_stage": null,
  "mode": "decide",
  "recommendation_json": "{\"suggested_mode\": \"decide\", \"reasoning\": \"...\", \"relevance_score\": 4}",
  "routing": "prd",
  "composite_score": 4.15,
  "created_at": "2026-05-24T10:00:00",
  "completed_at": "2026-05-24T10:12:34",
  "stage_outputs": [...],
  "gate3_review": {
    "routing": "poc",
    "composite_score": 4.3,
    "blocking_count": 1,
    "assumptions": [{"statement": "...", "severity": "Blocking", "reason": "..."}],
    "rationale": "...",
    "personas": [{"persona": "skeptic", "dimension": "Confidence", "score": 3, "key_argument": "..."}],
    "rubric_total": "11/12"
  }
}
```

`stage_outputs` is only present when `include_outputs=true`.
`gate3_review` is `null` until S5 has run, then present on every response
(US-30) — Hermes renders it in the Gate 3 notification follow-up and the PM
can inspect it when confirming or overriding routing.

---

### `GET /runs/{id}/artifacts`
List persisted artifacts for a run in reverse chronological order.

**Query parameters:**
- `artifact_type` — optional filter. Valid values:
  - `insight_memo` — signal insight summary (saved at S2)
  - `opportunity_memo` — opportunity framing (saved at S3)
  - `evaluation_brief` — 4-persona evaluation summary (saved at S4)
  - `decision_memo` — routing decision with scores and rationale (saved at S5)
  - `poc_plan` — minimum experiment design (saved at S6A)
  - `prd` — full PRD (saved at S6B)
  - `executive_summary` — final stakeholder report (saved at S7)
  - `checkpoint` — cumulative pipeline state snapshot (saved at S2, S3, S5)
- `limit` — max results (default 20)

**Response (200):**
```json
[
  {
    "artifact_id": "uuid",
    "run_id": "uuid",
    "type": "executive_summary",
    "content_md": "# Summary",
    "content_json": "{\"markdown\": \"# Summary\"}",
    "source_stage": "s7",
    "created_at": "2026-05-24T10:12:34"
  }
]
```

Hermes should prefer this endpoint when it only needs persisted Markdown/JSON artifacts
and does not need every stage output blob. Use `artifact_type=checkpoint` to retrieve
the latest pipeline state when a run is paused, killed, or still in progress.

---

### `POST /runs/start`
Start a pipeline run for an existing signal.

**Request body:**
```json
{
  "signal_id": "uuid",
  "product_id": "example-security-product",
  "mode": "decide"
}
```

`mode` is optional. If omitted, the run pauses after Stage 2 in `awaiting_direction`.

**Validation rules:**
- the referenced signal must exist, otherwise `404`
- `product_id` must exactly match the signal's stored `product_id`, otherwise `422`
- when valid, the signal's stored `product_id` is treated as canonical for run creation and downstream context loading

**Response (202):** Run object with status `running`.

---

### `POST /runs/{id}/direction`
Gate 1 response — confirm or override the suggested mode.

**Request body:**
```json
{ "mode": "decide" }
```

`mode`: `"file"` | `"brief"` | `"opportunity"` | `"evaluate"` | `"decide"`

**Response (202):** Run object with updated status.

---

### `POST /runs/{id}/approve`
Gate 2 — approve the S4 evaluation. Triggers S5 → S6 → S7.

**Request body:** `{}` (empty)

**Response (202):** `{ "run_id": "uuid", "action": "approved" }`

---

### `POST /runs/{id}/revise`
Gate 2 — request S4 re-evaluation with PM feedback.

**Request body:**
```json
{ "feedback": "The Skeptic underweighted the regulatory risk from DISA." }
```

**Response (202):** `{ "run_id": "uuid", "action": "revise_queued" }`

---

### `POST /runs/{id}/reject`
Gate 2 — kill the run at evaluation stage.

**Request body:**
```json
{ "reason": "Competitive signal is too early-stage to act on." }
```

**Response (200):** `{ "run_id": "uuid", "action": "rejected" }`

---

### `POST /runs/{id}/routing-review`
Gate 3 — confirm a kill decision or override to poc/prd.

**Request body (confirm kill):**
```json
{ "action": "confirm", "reason": "DISA mandate does not apply to our product segment." }
```

**Request body (override):**
```json
{ "action": "override", "routing": "prd", "reason": "RKP transition is worth a PRD." }
```

`action`: `"confirm"` | `"override"`
`routing` (required if action=override): `"poc"` | `"prd"`

**Response (202):**
```json
{ "run_id": "uuid", "action": "kill_confirmed" }
// or
{ "run_id": "uuid", "action": "routing_overridden", "routing": "prd" }
```

---

### `POST /runs/{id}/reopen`
Revive an auto-triaged run to `awaiting_direction` (US-31). Only runs that were
silently filed by the relevance gate are revivable — deliberate PM decisions
(file at Gate 1, reject at Gate 2, kill at Gate 3) return `409`.

**Response (200):** updated run object (`status=awaiting_direction`, `mode=null`,
`completed_at=null`); the signal returns to `status="in_run"`.

**Errors:** `404` unknown run · `409` not auto-triaged, or not in `completed` state.

**Digest ownership:** pm-engine sends no notification for auto-triaged runs.
The daily digest is Hermes-owned: poll `GET /runs?event=auto_triaged&since=<last-digest>`
and include the results in the hermes-eval / hermes-ops digest.

---

## Hermes Polling Pattern

Hermes should poll the following queues at a cadence suited to PM availability:

```
# Every N minutes (Hermes decision):
GET /runs?status=awaiting_direction    → notify PM, await direction input
GET /runs?status=waiting_approval      → send Gate 2 notification with review link
GET /runs?status=waiting_routing_review → send Gate 3 notification

# On terminal event / periodic:
GET /runs?status=completed             → read artifacts, perform wiki sync
GET /runs?status=killed                → record kill in wiki if applicable
```

**Do not poll `running` or `pending`** — these are transient and change without
Hermes intervention.

### Consumer defaults

- Sort assumption: responses are newest-first by `created_at`; Hermes should checkpoint by `run_id` + `completed_at`, not by array position.
- Deduping rule: a terminal run may be observed more than once; Hermes should treat wiki sync and notifications as idempotent.
- Artifact fetch rule: prefer `GET /runs/{id}/artifacts` for persisted artifacts; use `GET /runs/{id}?include_outputs=true` when stage-level detail is required.
- Auto-triage detection: Hermes should treat `status=completed` + `mode=file` + `recommendation_json` present as the canonical auto-triage signature.
- Recovery rule: if a run is `failed`, Hermes may notify or open an ops item, but must not mutate state except through documented gate endpoints.

---

## Error Responses

| Code | Meaning |
|------|---------|
| 401 | Missing, malformed, or invalid `Authorization: Bearer` token |
| 404 | Run or signal not found |
| 409 | Run is not in the expected state for this action |
| 422 | Invalid request body (e.g. unknown mode or routing value) |
| 503 | Server started without `PM_PLATFORM_API_TOKEN` set (fails closed) |

All errors return `{ "detail": "human-readable message" }`.
