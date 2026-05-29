# Architecture
## jackhpark-pm-agentic-platform

---

## 1. System Overview

This platform is the **workflow execution engine** for a personal PM intelligence system.
It is intentionally narrow: it runs stages, persists state, manages human gates, and exports
artifacts to the decision-system. Signal harvesting, wiki sync, and operational scheduling are
owned by the separate **Hermes operations plane**.

The engine runs on an always-on iMac. Tailscale makes it reachable from any device (iPhone,
MacBook) without port-forwarding. Gate notifications are fired directly by pm-platform;
Hermes may absorb this concern later.

```
┌─────────────────────────────────────────────────────────────────────────┐
│         jackhpark-pm-agentic-platform (ENGINE — hosted on iMac)         │
│                                                                         │
│   POST /signals ──▶  Signal DB  ──▶  S1–S7 Workflow  ──▶  Artifacts    │
│                                       (FastAPI + SQLite)                │
│                                             │                           │
│                                     Human gates (3)                    │
│                                      Gate 1: direction                  │
│                                      Gate 2: evaluation                 │
│                                      Gate 3: routing review             │
│                                             │                           │
│                                   run_finalizer.py                      │
│                                      ├─ completed_at stamp              │
│                                      └─ decision-system export          │
│                                             │                           │
│                          notifier.py (FanoutNotifier)                   │
│                           Gate 1 alert (Telegram/Slack)                 │
│                           Gate 2 alert + review page link               │
│                           Gate 3 alert (kill routing review)            │
└─────────────────────────────────────────────────────────────────────────┘
        ↑ read context / write runs              ↑ (export on completion)
decision-context-companion-repo/          decision-context-companion-repo/runs/

              │ Tailscale                    │ Gate 2 review link
              │ (iMac reachable from         │ http://<imac-tailscale-ip>:8000
              │  iPhone / MacBook)           │ /runs/{id}/review
              ▼                             ▼
        📱 Telegram / Slack           📱 Browser (PM's iPhone)
            notification               review page → Approve/Revise/Reject

┌─────────────────────────────────────────────────────────────────────────┐
│              External Operations Plane (Hermes — separate repo)         │
│                                                                         │
│   Signal harvesting (RSS, file watch)   Wiki sync (WIKI_ROOT writes)   │
│   Run monitoring / dashboards           Pattern accumulation            │
│   Operational scheduling (cron/harvest)                                 │
└─────────────────────────────────────────────────────────────────────────┘
        ↓ POST /signals                          ↓ wiki write (on run event)
jackhpark-pm-agentic-platform API        product-management-wiki-repo/

```

---

## 2. Responsibility Boundaries

| Concern | Owner | Notes |
|---------|-------|-------|
| Signal intake (manual) | pm-platform | `POST /signals` API |
| Signal harvesting (RSS, file watch) | Hermes | Submits via `POST /signals` |
| Stage execution (S1–S7) | pm-platform | Background tasks, async |
| Human gate state machine | pm-platform | 3 gates, 10 API endpoints |
| Persistence (runs, artifacts) | pm-platform | SQLite → PostgreSQL in v2 |
| decision-system export | pm-platform | `run_finalizer` triggers on decide-mode completion |
| Wiki sync | Hermes | Polls for completed/killed events, writes to WIKI_ROOT |
| Gate notifications | pm-platform | `notifier.py` fires Gate 1 alert (after S2), Gate 2 alert (after S4, includes link to `GET /runs/{id}/review`), and Gate 3 alert (after S5 routing=kill); Hermes bridges PM responses back via API |
| Operational scheduling | Hermes | Cron/harvest jobs |
| Pattern accumulation | Hermes | Reads completed runs, maintains wiki |

---

## 3. Layer Definitions

### ENGINE Layer (this repository)
The workflow execution runtime. Owns the database, API, and eval harness.

**Components:**
- FastAPI HTTP server
- SQLite database (v1 → PostgreSQL in v2)
- `run_finalizer` — single exit point for terminal transitions
- Eval harness for quality measurement

**Does NOT own:** signal scheduling, wiki writes, long-running cron jobs

### DECIDE Layer
The core workflow. A signal enters as raw text; a routing decision and artifact exit.

**Stages:** S1 → S2 → S3 → S4 (parallel multi-agent) → [PM Gate] → S5 → S6A or S6B → S7

**Output:** `StageOutput` records, `ApprovalEvent` records, `Artifact` records

### External Operations Plane (Hermes)
Handles everything that requires always-on or scheduled operation.

**Responsibilities:** RSS/file-watch signal harvesting, wiki sync, monitoring dashboards,
operational scheduling, pattern accumulation.

**Integration:** Hermes interacts with pm-platform exclusively via the HTTP API.
Direct database mutation or file-based approval are prohibited — see `docs/INTEGRATION_PRINCIPLES.md`.

**Note on notifications:** Gate 1, Gate 2, and Gate 3 alerts are fired by pm-platform's
built-in `FanoutNotifier`. Hermes-ops bridges PM responses back to pm-platform via the gate
API endpoints (`/direction`, `/approve`, `/revise`, `/reject`, `/routing-review`).

---

## 3. External Repository Integration

### decision-context-companion-repo

| Path | Usage | Direction |
|---|---|---|
| `core/00-pm-identity.md` | Loaded into every LLM call as system-level context | Read |
| `company-context.md` | Company strategy layer for context loading | Read |
| `products/<name>/context.md` | Product-specific context layer | Read |
| `products/<name>/signal-sources.md` | RSS/URL sources for signal collection | Read |
| `prompts/s1/` through `prompts/s7/` | Stage prompt templates | Read |
| `products/<name>/runs/<date>-<slug>/` | Exported run artifacts | Write |

### product-management-wiki-repo

| Path | Usage | Direction | Owner |
|---|---|---|---|
| `raw/from-web/sensing/` | Source of new signal files (Hermes watches) | Read (by Hermes) | Hermes |
| `raw/from-decision-system/kills/` | Kill decision S7 reports | Write | Hermes |
| `raw/from-decision-system/prds/` | PRD decision S7 reports | Write | Hermes |
| `raw/from-decision-system/poc-upgrades/` | PoC decision S7 reports | Write | Hermes |
| `raw/from-decision-system/kills/auto-triaged/` | Auto-triage archive | Write (transitional) | pm-platform → Hermes |

**pm-platform does not write to `WIKI_ROOT` as part of run completion.** Wiki writes are
Hermes-owned. The auto-triage archive path is the only exception and is transitional — see
`docs/EXPORT_AND_SYNC_CONTRACT.md`.

Both paths are configured in `config.py` as `DECISION_SYSTEM_ROOT` and `WIKI_ROOT`.

---

## 4. Component Map

```
app/
├── models/
│   ├── workflow.py        SQLAlchemy ORM models (WorkflowRun, Signal, StageOutput, ...)
│   └── stages.py          Pydantic I/O schemas for each stage (S1Input, S1Output, ...)
│
├── stages/                One function per stage: async def run(input, context, llm) -> Output
│   ├── s1_signal.py
│   ├── s2_insight.py
│   ├── s3_opportunity.py
│   ├── s4_evaluation.py   Orchestrates 4 persona agents in parallel (asyncio.gather)
│   ├── s5_prioritization.py
│   ├── s6a_poc_plan.py
│   ├── s6b_prd.py
│   └── s7_summary.py
│
├── agents/                Persona agent definitions used by s4_evaluation.py
│   ├── explorer.py        system_prompt + question + score extractor
│   ├── strategist.py
│   ├── builder.py
│   └── skeptic.py
│
├── services/
│   ├── context_loader.py  Loads 3-layer context from DECISION_SYSTEM_ROOT
│   ├── template_service.py Loads and renders prompt templates from /prompts/
│   ├── run_finalizer.py   Single exit point for terminal transitions; triggers export
│   ├── run_exporter.py    Writes completed runs to DECISION_SYSTEM_ROOT format
│   ├── notifier.py        FanoutNotifier: fires Gate 1 + Gate 2 alerts via Telegram/Slack
│   │                      Gate 2 alert includes link to /runs/{id}/review (see Section 11)
│   └── wiki_sync.py       Utility adapter (canonical paths); not called from completion paths
│                          (wiki writes are Hermes-owned — see EXPORT_AND_SYNC_CONTRACT.md)
│
├── storage/
│   ├── protocol.py        PMWorkflowStore Protocol (typed interface)
│   └── sqlite_store.py    SQLite implementation; auto-stamps completed_at on completed/killed
│
├── api/
│   ├── main.py            FastAPI app initialization
│   ├── signals.py         /signals routes
│   ├── runs.py            /runs routes + Gate 1 logic
│   ├── direction.py       /runs/{id}/direction — Gate 1 response
│   ├── approvals.py       /runs/{id}/approve|revise|reject — Gate 2
│   ├── routing_review.py  /runs/{id}/routing-review — Gate 3
│   ├── artifacts.py       /runs/{id}/artifacts — artifact query endpoint
│   └── review.py          /runs/{id}/review — browser-based Gate 2 review page
│                          (HTML; linked from Gate 2 Telegram notification; see Section 11)
│
├── llm/
│   ├── protocol.py        LLMProvider Protocol: async complete(messages) -> str
│   ├── claude.py          Anthropic Claude implementation
│   └── openai.py          OpenAI implementation
│
├── factory.py             build_engine(runtime) — wires all dependencies
└── logging.py             emit_event(stage, action, run_id, detail) — structured JSON
```

---

## 5. Data Model

### WorkflowRun
The central unit. One run = one signal going through the full workflow.

```
run_id          uuid, primary key
product_id      str (matches products/<name>/ directory name)
signal_id       uuid, foreign key → Signal
status          enum: pending | running | waiting_approval | completed | killed | failed
current_stage   str: s1 | s2 | s3 | s4 | s5 | s6a | s6b | s7
routing         enum: prd | poc | kill | null
composite_score float | null
created_at      datetime
completed_at    datetime | null
```

### Signal
```
signal_id       uuid, primary key
product_id      str
title           str
source_url      str | null
raw_content     text
category        enum: competitor | platform | regulation | technology | other
status          enum: pending | in_run | done
source_type     enum: manual | rss | file_watch
ingested_at     datetime
```

Signal lifecycle is runtime-maintained by pm-platform:
- `pending` immediately after intake
- `in_run` once a run starts successfully
- `done` when the run ends in `completed` or `killed`
- `pending` again when the run ends in `failed`, so the signal returns to the retryable pool

### StageOutput
```
output_id       uuid, primary key
run_id          uuid, foreign key → WorkflowRun
stage           str: s1 | s2 | s3 | s4 | s5 | s6a | s6b | s7
output_json     text (Pydantic model serialized to JSON)
version         int (increments on revise)
created_at      datetime
```

### ApprovalEvent
```
event_id        uuid, primary key
run_id          uuid, foreign key → WorkflowRun
stage           str (the stage being approved / revised)
action          enum: approve | revise | reject
feedback_text   text | null
created_at      datetime
```

### Artifact
```
artifact_id     uuid, primary key
run_id          uuid, foreign key → WorkflowRun
type            enum: poc_plan | prd | executive_summary
content_md      text (Markdown)
content_json    text (structured JSON)
created_at      datetime
```

---

## 6. State Machine (WorkflowRun.status)

```
  POST /runs/start
        │
        ▼
    pending ──▶ running ──── exception ──▶ failed
                  │
          [S2 relevance < threshold]
                  │
                  ▼
            completed (auto-triage, mode=file)
                  │
          [relevance OK, no mode set]
                  │
                  ▼
        awaiting_direction   ←── Gate 1: POST /runs/{id}/direction
                  │
          [direction given]
                  ▼
              running
               mode?
          ┌────────────────────────┐
          ▼                        ▼
       file/brief/               decide
       opp/eval              S4 completes
          │                        ▼
          ▼              waiting_approval  ←── Gate 2: approve/revise/reject
       completed               │
                    ┌──────────┼──────────┐
                    ▼          ▼          ▼
                 killed    running     running
                (reject)  (revise,   (approve →
                          S4 retry)   S5 runs)
                                        │
                                   S5 routes
                                  ┌─────┴──────┐
                                  ▼            ▼
                                kill         prd/poc
                                  │            │
                                  ▼            ▼
                      waiting_routing_review   S6+S7
                      ←── Gate 3: confirm/     │
                           override            ▼
                          ┌──────┐         completed
                          ▼      ▼
                        killed  running
                        (conf)  (override → S6+S7 → completed)
```

**Terminal states and `completed_at` policy:**

| Status | `completed_at` | Export to decision-system |
|--------|---------------|--------------------------|
| `completed` | ✅ auto-stamped | decide mode only (via `run_finalizer`) |
| `killed` | ✅ auto-stamped | never |
| `failed` | ❌ intentionally unset | never |

---

## 7. LLM Provider Abstraction

All LLM calls go through a `LLMProvider` protocol. No stage function imports an SDK directly.

```python
# app/llm/protocol.py
from typing import Protocol

class Message(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str

class LLMProvider(Protocol):
    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 2048,
    ) -> str: ...
```

**Provider selection:** `LLM_PROVIDER` environment variable (`claude` | `openai`). Default: `claude`.

**Swap example:**
```bash
# .env
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
```

---

## 8. S4 Multi-Agent Execution

S4 is the only stage with parallel sub-agents. Each persona agent is defined by:
- A system prompt describing its role and evaluation frame
- A fixed question it must answer (not a conversational prompt)
- A structured output schema: `{ score: int, key_argument: str, open_question: str }`

Execution:
```python
# Simplified from app/stages/s4_evaluation.py
results = await asyncio.gather(
    explorer.evaluate(opportunity, context, llm),
    strategist.evaluate(opportunity, context, llm),
    builder.evaluate(opportunity, context, llm),
    skeptic.evaluate(opportunity, context, llm),
)
```

Results are stored independently. No synthesis happens here — that is S5's job.

---

## 9. Eval Harness

```
eval/
├── scenarios.json         Array of scenario objects
│                          { signal, product_id, expected_routing, expected_score_range }
├── runner.py              Loads scenarios, runs full S1–S5, compares to expected
└── rubrics/
    ├── s4_rubric.py       12-point checklist evaluator
    └── s5_routing.py      Routing accuracy checker
```

**Golden dataset:** R01–R07 from `example-security-product`:

| Run | Signal | Expected Routing | Expected Composite |
|---|---|---|---|
| R04 | Android 16 APM enforcement | PRD | ~4.30 |
| R05 | Android 16 NFC allowlist | PRD | ~4.50 |
| R06 | DISA MTD mandate | Kill | ~1.35 |
| R07 | Android 16 RKP transition | PRD | ~4.30 |

---

## 10. Upgrade Path

| Version | Changes |
|---|---|
| v1 (current) | SQLite, manual trigger, Hermes integration contract, eval harness |
| v2 | PostgreSQL, run history UI, Hermes harvesting live |
| v3 | LangGraph for complex branching, if needed |
| v4 | Wiki semantic search (RAG) for context injection |
| v5 | Multi-user, Prefect/Temporal for durable execution |

**Note:** RSS/file-watch signal harvesting and operational scheduling were originally planned
as in-process features (APScheduler). These have been moved to the Hermes operations plane.
The pm-platform API (`POST /signals`) remains the stable integration point.

---

## 11. Human-in-the-Loop Notification and Review Flow

### Overview

pm-platform fires Gate notifications directly via `app/services/notifier.py`. Hermes does not
participate in the notification loop. The Gate 2 notification includes a link to a browser-based
review page, accessible from any device connected to the same Tailscale network.

### Hosting and Tailscale

pm-platform runs on an always-on **iMac**. A MacBook is unsuitable as a host because closing
the lid suspends the process, breaking Hermes polling and making review links unreachable.

**Tailscale** is installed on the iMac and the PM's iPhone (and optionally MacBook). Tailscale
assigns the iMac a stable private IP (e.g. `100.x.x.x`) that is reachable from any device in
the same Tailnet regardless of network location.

`BASE_URL` in `.env` should be set to the iMac's Tailscale IP:

```
BASE_URL=http://100.x.x.x:8000
```

With this set, the review page link embedded in every Gate 2 Telegram notification remains
valid whether the PM is at home, in transit, or on a different network.

### Gate 1 Notification (after S2)

Fired by `FanoutNotifier.send_gate1()` when a signal passes the auto-triage threshold and
pm-platform is waiting for the PM to choose a run mode.

**Content:**
- Product name and signal title
- Relevance score (S2 output)
- Suggested mode (S2 recommendation)
- Exact API call to start the run (`POST /runs/start`)

**PM action:** Call the API (via curl, Shortcuts, or Hermes) to start the run with a chosen mode.

### Gate 2 Notification (after S4)

Fired by `FanoutNotifier.send_gate2()` after the four persona agents complete their evaluations
and the run enters `waiting_approval` state.

**Content:**
- Product name and run ID
- Scores for all four personas (Explorer / Strategist / Builder / Skeptic)
- Skeptic's key concern (most likely reason for doubt)
- Link to the browser review page: `{BASE_URL}/runs/{run_id}/review`

**PM action:** Tap the review link → review persona cards in browser → tap Approve, Revise, or Reject.

### Review Page (`GET /runs/{id}/review`)

Served by `app/api/review.py`. Renders an HTML page showing:
- All four persona evaluations with color-coded score cards
- Approve / Revise / Reject action buttons (POST to the JSON API via fetch)
- Revise requires a feedback text field (used to re-run S4 with PM direction)
- Page becomes read-only after any action is taken (shows current status)

### End-to-End Loop

```
[S2 completes — relevance passes threshold]
        │
        ▼
FanoutNotifier.send_gate1()
  → Telegram: "New signal: <title> — relevance 4/5 — POST /runs/start"
        │
        ▼ [PM starts run via API]
[S3 → S4 complete — 4 personas scored]
        │
        ▼
FanoutNotifier.send_gate2()
  → Telegram: "Gate 2 ready — Skeptic: 'no admin API' — 🔗 review link"
        │
        ▼ [PM taps link → iPhone browser opens review page via Tailscale]
GET /runs/{id}/review   (served by iMac over Tailscale)
        │
   PM taps Approve
        │
        ▼
POST /runs/{id}/approve
  → S5 → S6 → S7 → completed
  → run_finalizer exports to DECISION_SYSTEM_ROOT
        │
        ▼ [Hermes polls GET /runs?status=completed]
Hermes reads artifacts → writes to WIKI_ROOT (Hermes-owned)
```

### Configuration Reference

| Setting | Description | Example |
|---------|-------------|---------|
| `BASE_URL` | iMac's Tailscale URL; used to construct review page links | `http://100.x.x.x:8000` |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather | `7123456789:AAF...` |
| `TELEGRAM_CHAT_ID` | Chat ID where alerts are sent (personal or group) | `123456789` |
| `SLACK_WEBHOOK_URL` | Incoming Webhook URL from Slack app settings; leave empty to disable | `https://hooks.slack.com/...` |

All four settings live in `.env`. Leave `TELEGRAM_BOT_TOKEN` or `SLACK_WEBHOOK_URL` empty to
disable that provider. Both providers can be active simultaneously via `FanoutNotifier`.

### iMac Setup Checklist

1. Clone this repo on the iMac: `git clone ...`
2. Clone external repos at the same paths configured in `.env`:
   - `decision-context-companion-repo` → `DECISION_SYSTEM_ROOT`
   - `product-management-wiki-repo` → `WIKI_ROOT`
3. Install Tailscale on the iMac and ensure it is running
4. Set `BASE_URL=http://<imac-tailscale-ip>:8000` in `.env`
5. Set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` in `.env`
6. Start the API: `uvicorn app.api.main:app --reload` (or via launchd for auto-start)
7. Install Tailscale on iPhone — verify `BASE_URL` is reachable from iPhone Safari
