# API Contract
## jackhpark-pm-agentic-platform — Public Interface for Hermes

**Version:** 1.0  
**Last updated:** 2026-05-24  
**Base URL:** configured via `BASE_URL` in `.env` (default `http://localhost:8000`)

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
| `GET` | `/runs/{id}/review` | Gate 2 browser review page | Link in Gate 2 notification |
| `GET` | `/health` | Health check | Liveness probe |

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

---

### `GET /runs`
List runs. Hermes uses this for polling actionable queues.

**Query parameters:**
- `product_id` — filter by product
- `status` — filter by status (e.g. `awaiting_direction`, `completed`)
- `routing` — filter by routing (`prd`, `poc`, `kill`)
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
  "stage_outputs": [...]
}
```

`stage_outputs` is only present when `include_outputs=true`.

---

### `GET /runs/{id}/artifacts`
List persisted artifacts for a run in reverse chronological order.

**Query parameters:**
- `artifact_type` — optional filter (`executive_summary`, `prd`, `poc_plan`)
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
    "created_at": "2026-05-24T10:12:34"
  }
]
```

Hermes should prefer this endpoint when it only needs persisted Markdown/JSON artifacts
and does not need every stage output blob.

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
| 404 | Run or signal not found |
| 409 | Run is not in the expected state for this action |
| 422 | Invalid request body (e.g. unknown mode or routing value) |

All errors return `{ "detail": "human-readable message" }`.
