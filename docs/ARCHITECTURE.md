# Architecture
## jackhpark-pm-agentic-platform

---

## 1. System Overview

This platform is the **workflow execution engine** for a personal PM intelligence system.
It is intentionally narrow: it runs stages, persists state, manages human gates, and exports
artifacts to the decision-system. Signal harvesting, wiki sync, notifications, and operational
scheduling are owned by a separate **Hermes operations plane**.

```
┌─────────────────────────────────────────────────────────────────────────┐
│              jackhpark-pm-agentic-platform (ENGINE)                     │
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
└─────────────────────────────────────────────────────────────────────────┘
        ↑ read context / write runs              ↑ (export on completion)
jackhpark-pm-decision-system/          jackhpark-pm-decision-system/runs/

┌─────────────────────────────────────────────────────────────────────────┐
│              External Operations Plane (Hermes — separate repo)         │
│                                                                         │
│   Signal harvesting (RSS, file watch)   Wiki sync (WIKI_ROOT writes)   │
│   Gate notifications (Telegram, Slack)  Run monitoring / dashboards     │
│   Operational scheduling (cron/harvest) Pattern accumulation            │
└─────────────────────────────────────────────────────────────────────────┘
        ↓ POST /signals                          ↓ wiki write (on run event)
jackhpark-pm-agentic-platform API        jackhpark-product-management-wiki/

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
| Gate notifications | pm-platform | FanoutNotifier (Telegram/Slack) for Gates 1 and 2 |
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
notification bridging for mobile approval flows, operational scheduling.

**Integration:** Hermes interacts with pm-platform exclusively via the HTTP API.
Direct database mutation or file-based approval are prohibited — see `docs/INTEGRATION_PRINCIPLES.md`.

---

## 3. External Repository Integration

### jackhpark-pm-decision-system

| Path | Usage | Direction |
|---|---|---|
| `core/00-pm-identity.md` | Loaded into every LLM call as system-level context | Read |
| `company-context.md` | Company strategy layer for context loading | Read |
| `products/<name>/context.md` | Product-specific context layer | Read |
| `products/<name>/signal-sources.md` | RSS/URL sources for signal collection | Read |
| `prompts/s1/` through `prompts/s7/` | Stage prompt templates | Read |
| `products/<name>/runs/<date>-<slug>/` | Exported run artifacts | Write |

### jackhpark-product-management-wiki

| Path | Usage | Direction |
|---|---|---|
| `raw/from-web/sensing/` | Watched for new signal files | Read |
| `raw/from-decision-system/kills/` | Kill decision S7 reports | Write |
| `raw/from-decision-system/prds/` | PRD decision S7 reports | Write |
| `raw/from-decision-system/poc-upgrades/` | PoC decision S7 reports | Write |

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
│   └── review.py          /runs/{id}/review — browser-based Gate 2 review page
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

**Golden dataset:** R01–R07 from `samsung-knox-lockdown-mode`:

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
