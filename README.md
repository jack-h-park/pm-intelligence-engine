# jackhpark-pm-agentic-platform

A personal PM intelligence platform that automates the signal-to-decision workflow, so a PM can focus entirely on judgment — not on collecting, formatting, or filing information.

## What It Does

This platform connects three layers of PM work into a single automated pipeline:

```
SENSE ──▶ DECIDE ──▶ LEARN
```

- **SENSE**: Collects external market and competitor signals (RSS feeds, manual input, wiki sensing files)
- **DECIDE**: Runs signals through a structured 7-stage analysis workflow (S1–S7) with a multi-agent persona evaluation and a human approval gate
- **LEARN**: Syncs completed decision records back to the PM wiki for long-term pattern accumulation

## Related Projects

| Project | Role |
|---|---|
| [`jackhpark-pm-decision-system`](../../ai-assets/jackhpark-pm-decision-system/) | Source of workflow design, prompt templates, product contexts, and run history |
| [`jackhpark-product-management-wiki`](../../ai-assets/jackhpark-product-management-wiki/) | Source of external signals; destination for completed decision ingest |
| [`ai-agent-test`](../../forks/ai-agent-test/) | Reference implementation for agentic patterns (not reused directly) |

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│                 jackhpark-pm-agentic-platform                        │
│                                                                      │
│   ┌────────────────┐     ┌──────────────────┐     ┌──────────────┐  │
│   │  SENSE Layer   │────▶│  DECIDE Layer    │────▶│  LEARN Layer │  │
│   │                │     │                  │     │              │  │
│   │ Signal ingestion│    │  S1~S7 Workflow  │     │  Wiki sync   │  │
│   │ RSS / manual   │     │  Multi-agent S4  │     │  Pattern     │  │
│   │ File watch     │     │  Human gate (S4) │     │  accumulation│  │
│   └────────────────┘     └────────┬─────────┘     └──────────────┘  │
│                                   │                                  │
│                    ┌──────────────▼───────────────┐                 │
│                    │         ENGINE Layer          │                 │
│                    │   FastAPI + SQLite            │                 │
│                    │   APScheduler + Eval Harness  │                 │
│                    └───────────────────────────────┘                │
└──────────────────────────────────────────────────────────────────────┘
         ↑ read context / write runs       ↑ read signals / write ingest
 jackhpark-pm-decision-system/     jackhpark-product-management-wiki/
```

## The 7-Stage Workflow

Each signal runs through a sequential pipeline:

| Stage | Name | LLM | Description |
|---|---|---|---|
| S1 | Signal | No | Normalize raw input into a structured, factual signal record |
| S2 | Insight | Yes | Extract "what changed" and connect it to a strategy pillar |
| S3 | Opportunity | Yes | Frame a falsifiable hypothesis with target user and assumed value |
| S4 | Evaluation | Yes × 4 | Four independent persona agents (Explorer / Strategist / Builder / Skeptic) evaluate in parallel |
| S5 | Prioritization | Partial | Weighted composite score → routing decision (PRD / PoC / Kill) |
| S6A | PoC Plan | Yes | Minimum experiment design for low-confidence opportunities |
| S6B | PRD | Yes | Structured product requirements document for high-confidence opportunities |
| S7 | Executive Summary | Yes | One-page narrative brief for stakeholders |

**Human gate:** After S4, the PM reviews all four persona evaluations before S5 executes.

## Key Design Principles

1. **Judgment traceability over speed** — every recommendation ships with its evidence chain
2. **PM approves, system executes** — automation never bypasses human judgment
3. **Vendor-agnostic LLM** — `LLMProvider` protocol; swap Claude / OpenAI / Gemini via config
4. **Measure from day one** — eval harness built before feature code, using real historical runs as golden data
5. **Stage functions, not conversational agents** — each stage has a typed input/output contract

## Project Structure

```
jackhpark-pm-agentic-platform/
├── config.py                  # Paths to external repos (DECISION_SYSTEM_ROOT, WIKI_ROOT)
├── app/
│   ├── models/                # SQLAlchemy DB models + Pydantic stage schemas
│   ├── stages/                # S1–S7 stage execution functions
│   ├── agents/                # S4 persona agent definitions
│   ├── services/              # Context loader, template service, signal collector, wiki sync
│   ├── storage/               # PMWorkflowStore Protocol + SQLite implementation
│   ├── api/                   # FastAPI routes (signals, runs, approvals)
│   ├── factory.py             # build_engine(runtime) dependency wiring
│   └── logging.py             # Structured JSON event logging
├── eval/
│   ├── scenarios.json         # Golden dataset from historical runs (R01–R07)
│   ├── runner.py              # Full scenario regression runner
│   └── rubrics/               # Stage-specific quality evaluators
├── tests/
│   ├── unit/
│   └── integration/
├── docs/
│   ├── PRD.md
│   ├── ARCHITECTURE.md
│   ├── DESIGN_DECISIONS.md
│   └── IMPLEMENTATION_PLAN.md
└── pyproject.toml
```

## Setup

```bash
# Clone and navigate
cd /Users/jackpark/workspace/code/core/jackhpark-pm-agentic-platform

# Install dependencies
pip install -e ".[dev]"

# Set environment variables
cp .env.example .env
# Edit .env: LLM_PROVIDER, ANTHROPIC_API_KEY or OPENAI_API_KEY

# Run the API
uvicorn app.api.main:app --reload

# Run eval harness
python eval/runner.py
```

## Documentation

- [PRD](docs/PRD.md) — problem definition, features, success metrics
- [Architecture](docs/ARCHITECTURE.md) — system design, data model, integration points
- [Design Decisions](docs/DESIGN_DECISIONS.md) — why this is different from `ai-agent-test`
- [Implementation Plan](docs/IMPLEMENTATION_PLAN.md) — phase-by-phase build order
