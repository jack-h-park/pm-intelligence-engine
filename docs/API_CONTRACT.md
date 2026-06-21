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
| `POST` | `/signals/reconcile` | Re-derive every signal's status from its runs | Maintenance / drift repair |
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
| `waiting_direction` | Gate 1 | `POST /runs/{id}/direction` | `GET /runs?status=waiting_direction` |
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
  "original_product_id": "example-security-product",
  "title": "Android 16 NFC admin control API released",
  "raw_content": "Full article text or summary...",
  "source_url": "https://example.com/article",
  "source_type": "rss"
}
```

`original_product_id` is an **optional** origin/provenance hint (US-49) — `null` for
product-agnostic intake (RSS / file_watch), where Portfolio Triage routes the signal.
**`product_id` is accepted as a deprecated alias** so existing clients (Hermes) keep
working. `source_type`: `"manual"` | `"rss"` | `"file_watch"`

**Response (201):**
```json
{ "signal_id": "uuid", "original_product_id": "...", "title": "...", "status": "new" }
```

**Signal lifecycle:** `signals.status` is **derived** from the signal's runs, not
pushed independently — so it stays correct under fan-out (multiple runs per signal)
and is repairable if a run is ever created outside the normal finalize path:
- no runs yet → `new`; a successful `POST /runs/start` moves the signal to `in_run`
- **any** run still non-terminal → `in_run` (a completed sibling does not flip it early)
- all runs terminal, at least one `completed`/`killed` → `done`
- all runs `failed`, a retry lineage hit `MAX_RUN_ATTEMPTS` → `blocked`; otherwise → `new` (retryable)

The derivation lives in `app/services/signal_status.py`; `finalize_run` reconciles
on every terminal transition.

---

### `POST /signals/reconcile`
Re-derive **every** signal's status from its runs and persist any divergence.
Idempotent and safe to re-run. Use it to repair rows that drifted before the
derived-status invariant existed — e.g. a signal left at `in_run` by a
synthetic/backfilled `completed` run that bypassed `finalize_run`.

**Response (200):**
```json
{
  "checked": 142,
  "corrected": 1,
  "changes": [
    { "signal_id": "uuid", "title": "...", "old": "in_run", "new": "done" }
  ]
}
```

---

### `GET /runs`
List runs. Hermes uses this for polling actionable queues.

**Query parameters:**
- `product_id` — filter by product
- `status` — filter by status (e.g. `waiting_direction`, `completed`)
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
  "depth": "decide",
  "mode": "decide",
  "recommendation_json": "{\"suggested_mode\": \"decide\", \"reasoning\": \"...\", \"relevance_score\": 4}",
  "routing": "prd",
  "composite_score": 4.15,
  "created_at": "2026-05-24T10:00:00",
  "updated_at": "2026-05-24T10:12:34",
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
Start a pipeline run for an existing signal. Two modes (US-49):

**Manual (single product)** — provide `product_id`:
```json
{
  "signal_id": "uuid",
  "product_id": "example-security-product",
  "depth": "decide"
}
```

**Fan-out (Portfolio Triage)** — omit `product_id`:
```json
{ "signal_id": "uuid" }
```
Portfolio Triage scores the signal against every product profile in one call and a
run is started for each product whose relevance is at or above
`TRIAGE_RELEVANCE_THRESHOLD`. The spawned runs share a `batch_id`.

`depth` is the processing depth — one of `archive | note | structure | evaluate | decide`
(the depth ladder; canonical definition in
`pm-decision-context/core/02-workflow.md`). Optional; if omitted, runs pause after
Stage 2 in `waiting_direction`. **`mode` is accepted as a deprecated alias** of `depth`
(both the key `mode` and legacy values `file`/`brief`/`opportunity` are normalized), so
existing clients keep working. Responses include both `depth` and `mode`.

**Validation rules:**
- the referenced signal must exist, otherwise `404`
- **manual:** `product_id` must resolve to a product context directory, otherwise `422`.
  It is no longer required to match the signal's origin — `original_product_id` is only a
  hint under 1:N; the caller may name any existing product.

**Response (202):**
- **manual:** a single Run object with status `running` (back-compat — includes a new
  `batch_id` field, `null` for a single run).
- **fan-out:** `{ "batch_id": "...", "runs": [Run, ...], "triage": [{product_id,
  relevance_score, reason, relevant}, ...] }`. `runs` is empty when no product clears the
  threshold; `triage` always reports every product's verdict.

---

### `POST /runs/{id}/scan`
Manual Portfolio Scan (US-49) — human-in-the-loop fan-out for a run started for a single
product. Checks whether the same signal is relevant to **other** products and fans out to
them. A deliberate PM action (surfaced as a button on the Gate 1 review), not automatic;
works whether the origin run is paused at Gate 1 or already auto-triaged.

No request body.

**Behaviour:** Portfolio Triage runs over the portfolio **minus the origin product**. If
any other product clears `TRIAGE_RELEVANCE_THRESHOLD`, the origin run is pulled into a new
batch alongside the new sibling runs and membership is closed (so Variant 2 synthesis fires
once they all settle). If nothing else is relevant, the origin run is left untouched.

**Validation rules:**
- the run must exist, otherwise `404`
- the run must not already belong to a batch, otherwise `409`

**Response (202):** `{ "scanned_run_id": "...", "batch_id": "..."|null, "runs": [Run, ...],
"triage": [...] }`. `batch_id` is `null` (and `runs` empty) when no other product is
relevant; the spawned siblings do not repeat the origin run.

---

### `GET /runs/batch/{batch_id}`
A fan-out batch (US-49) — its sibling runs plus the post-hoc portfolio synthesis.

**Response (200):** `{ "batch_id": "...", "signal_id": "...", "membership_closed": bool,
"runs": [Run, ...], "synthesis": {...}|null }`. `synthesis` is `null` until every run in
the batch has settled; once present it carries the rendered memo (`content_md`) and the
structured `content` (priority ranking, root cause, sequencing, conflicts, synergies).
`404` if the batch does not exist.

---

### `POST /runs/{id}/direction`
Gate 1 response — confirm or override the suggested processing depth.

**Request body:**
```json
{ "depth": "decide" }
```
(`mode` accepted as a deprecated alias.)

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
Revive an auto-triaged run to `waiting_direction` (US-31). Only runs that were
silently filed by the relevance gate are revivable — deliberate PM decisions
(file at Gate 1, reject at Gate 2, kill at Gate 3) return `409`.

**Response (200):** updated run object (`status=waiting_direction`, `mode=null`,
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
GET /runs?status=waiting_direction    → notify PM, await direction input
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
