# Architecture
## jackhpark-pm-agentic-platform

---

## 1. System Overview

This platform is structured as four conceptual layers. Three of them already exist as separate repositories; this project implements the fourth (ENGINE) and wires them together.

```
┌──────────────────────────────────────────────────────────────────────┐
│                 jackhpark-pm-agentic-platform                        │
│                                                                      │
│   ┌────────────────┐     ┌──────────────────┐     ┌──────────────┐  │
│   │  SENSE Layer   │────▶│  DECIDE Layer    │────▶│  LEARN Layer │  │
│   │                │     │                  │     │              │  │
│   │ Signal ingestion│    │  S1–S7 Workflow  │     │  Wiki sync   │  │
│   │ RSS / manual   │     │  Multi-agent S4  │     │  Pattern     │  │
│   │ File watch     │     │  Human gate      │     │  accumulation│  │
│   └────────────────┘     └────────┬─────────┘     └──────────────┘  │
│                                   │                                  │
│                    ┌──────────────▼───────────────┐                 │
│                    │         ENGINE Layer          │                 │
│                    │   FastAPI + SQLite            │                 │
│                    │   APScheduler + Eval Harness  │                 │
│                    └───────────────────────────────┘                │
└──────────────────────────────────────────────────────────────────────┘
         ↑ read context / write runs       ↑ read signals / write ingest
 decision-context-companion-repo/     product-management-wiki-repo/
```

---

## 2. Layer Definitions

### SENSE Layer
Responsible for bringing external signals into the system.

**Sources:**
- Manual input via `POST /signals`
- RSS/URL polling (APScheduler, daily)
- File watch on `wiki/raw/from-web/sensing/`

**Output:** `Signal` records in the database

### DECIDE Layer
The core workflow. A signal enters as raw text; a routing decision and artifact exit.

**Design:** The workflow design already exists in `decision-context-companion-repo`. The ENGINE does not redesign it — it automates it.

**Stages:** S1 → S2 → S3 → S4 (parallel multi-agent) → [PM Gate] → S5 → S6A or S6B → S7

**Output:** `StageOutput` records, `ApprovalEvent` records, `Artifact` records

### LEARN Layer
Feeds completed decisions back into the PM knowledge base for long-term pattern accumulation.

**Behavior:** On S7 completion, the ENGINE copies the report to the wiki with YAML frontmatter.

**Output:** Files written to `wiki/raw/from-decision-system/`

### ENGINE Layer
The automation runtime. Owns the database, API, scheduler, and eval harness.

**Components:**
- FastAPI HTTP server
- SQLite database (v1)
- APScheduler for signal collection
- Eval harness for quality measurement

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
│   ├── signal_collector.py RSS polling + sensing file watcher
│   └── wiki_sync.py       Writes S7 output to WIKI_ROOT with frontmatter
│
├── storage/
│   ├── protocol.py        PMWorkflowStore Protocol (typed interface)
│   └── sqlite_store.py    SQLite implementation of the protocol
│
├── api/
│   ├── main.py            FastAPI app initialization
│   ├── signals.py         /signals routes (F1, F3)
│   ├── runs.py            /runs routes (F4, F11)
│   └── approvals.py       /runs/{id}/approve|revise|reject (F6)
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
                    pending
                        │
                   S1 starts
                        ▼
                    running ──── error ──▶ failed
                        │
               S4 completes
                        ▼
              waiting_approval ◀────── revise (loops back)
                        │
              PM: approve / reject
                ┌───────┴────────┐
                ▼                ▼
            running           killed
                │
           S5 routes
          ┌────┼─────┐
          ▼    ▼     ▼
        Kill  PoC   PRD
          │    │     │
          ▼    ▼     ▼
        killed running running
                │     │
                ▼     ▼
            S7 complete
                │
                ▼
            completed
```

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
| v1 (current) | SQLite, manual trigger, basic eval harness |
| v2 | PostgreSQL, RSS auto-collection, run history UI |
| v3 | LangGraph for complex branching, if needed |
| v4 | Slack notifications, wiki semantic search (RAG) |
| v5 | Multi-user, Prefect/Temporal for durable execution |
