# jackhpark-pm-agentic-platform

A personal PM intelligence platform that executes a structured signal-to-decision workflow, so a PM can focus on judgment instead of manual orchestration.

## What It Does

This platform executes the decision workflow and exposes the public surface that external operators use:

```
SENSE ──▶ DECIDE ──▶ LEARN
```

- **SENSE**: Accepts signals through the API
- **DECIDE**: Runs signals through a structured 7-stage analysis workflow (S1–S7) with a multi-agent persona evaluation and human gates
- **LEARN**: Exports decision artifacts and exposes terminal run state for external sync

## Related Projects

| Project | Role |
|---|---|
| [`decision-context-companion-repo`](../../ai-assets/decision-context-companion-repo/) | Source of workflow design, prompt templates, product contexts, and run history |
| [`product-management-wiki-repo`](../../ai-assets/product-management-wiki-repo/) | Source of external signals; destination for completed decision ingest |
| [`jackhpark-hermes-control-plane`](../../ai-assets/jackhpark-hermes-control-plane/) | External operations plane: harvesting, notifications, wiki sync, scheduling |
| [`jackhpark-notion-cms-backup`](../../data/jackhpark-notion-cms-backup/) | Notion → wiki raw 레이어 자동 동기화 (pm-platform과 직접 연결 없음, wiki를 통해 간접 영향) |
| [`ai-agent-test`](../../forks/ai-agent-test/) | Reference implementation for agentic patterns (not reused directly) |

> 전체 스택 아키텍처: [hermes-control-plane/docs/system-overview.md](../../ai-assets/jackhpark-hermes-control-plane/docs/system-overview.md)

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│                 jackhpark-pm-agentic-platform                        │
│                                                                      │
│   ┌──────────────────────┐     ┌─────────────────────────────────┐  │
│   │ Public API Surface   │────▶│ Workflow Engine                 │  │
│   │                      │     │                                 │  │
│   │ POST /signals        │     │ S1~S7 execution                 │  │
│   │ POST /runs/start     │     │ Gate state machine              │  │
│   │ GET /runs            │     │ SQLite persistence              │  │
│   │ Gate action routes   │     │ decision-system export          │  │
│   └──────────────────────┘     └─────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
              ▲ read context / write runs         ▲ poll / notify / sync
 decision-context-companion-repo/      Hermes operations plane + product wiki
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

## Ownership Boundaries

- `pm-platform` owns workflow execution, stage outputs, terminal state, decision-system export, and Gate 1/2 notifications.
- Hermes owns signal harvesting, scheduling, wiki sync, and long-running operations.
- Gate notifications (Telegram/Slack) are fired by pm-platform's built-in `FanoutNotifier`. Hermes may absorb this in a later version.
- Auto-triage archive is transitional: local archive writes remain available behind `AUTO_TRIAGE_LOCAL_ARCHIVE_ENABLED` until Hermes takes over fully.

## Hosting

pm-platform runs on an always-on **iMac**. Running on a MacBook is not recommended — lid-close
suspends the process, breaking Hermes polling and making Gate 2 review links unreachable.

**Tailscale** connects the iMac, iPhone, and MacBook in a private network. `BASE_URL` in `.env`
should be set to the iMac's Tailscale IP so that Gate 2 review page links work from iPhone even
when away from home. See `docs/ARCHITECTURE.md` Section 11 for the full notification flow and
iMac setup checklist.

## Project Structure

```
jackhpark-pm-agentic-platform/
├── config.py                  # Paths to external repos (DECISION_SYSTEM_ROOT, WIKI_ROOT)
├── app/
│   ├── models/                # SQLAlchemy DB models + Pydantic stage schemas
│   ├── stages/                # S1–S7 stage execution functions
│   ├── agents/                # S4 persona agent definitions
│   ├── services/              # Context loader, template service, export/finalization utilities
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
# Clone on the iMac and navigate
cd /Users/jackpark/workspace/code/core/jackhpark-pm-agentic-platform

# Install dependencies
pip install -e ".[dev]"

# Set environment variables
cp .env.example .env
# Required: LLM_PROVIDER, ANTHROPIC_API_KEY or OPENAI_API_KEY
# Required for notifications: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
# Required for mobile review: BASE_URL=http://<imac-tailscale-ip>:8000
# Optional: SLACK_WEBHOOK_URL
# Optional: set AUTO_TRIAGE_LOCAL_ARCHIVE_ENABLED=false after Hermes owns auto-triage archive

# Ensure external repos are cloned at the paths set in DECISION_SYSTEM_ROOT / WIKI_ROOT
```

## Operations

All server commands are available via `make`:

| Command | Description |
|---------|-------------|
| `make start` | Start server in background; logs → `logs/server.log` |
| `make stop` | Stop background server |
| `make restart` | Stop then start |
| `make status` | Show whether server is running and its PID |
| `make logs` | `tail -f logs/server.log` |
| `make dev` | Start in foreground with `--reload` (development) |
| `make test` | Run unit + integration test suite |
| `make eval` | Run eval harness |
| `make lint` | Run ruff linter |

**iMac auto-start (one-time setup):**

```bash
# Before installing, verify the uvicorn path in deploy/com.jackpark.pm-platform.plist
# matches `which uvicorn` on the iMac.

make install-service     # registers launchd agent; starts on login, auto-restarts on crash
make uninstall-service   # removes the launchd agent
```

See `docs/ARCHITECTURE.md` Section 11 for the full hosting, Tailscale, and notification setup.

## Documentation

- [PRD](docs/PRD.md) — problem definition, features, success metrics
- [Reverse PRD](docs/PRD_REVERSE.md) — current product view including implemented capabilities
- [Implementation Status](docs/IMPLEMENTATION_STATUS.md) — reverse PRD to code mapping and evidence
- [Reverse Roadmap](docs/ROADMAP_REVERSE.md) — remaining gaps and sequencing after the current baseline
- [Architecture](docs/ARCHITECTURE.md) — system design, data model, integration points
- [API Contract](docs/API_CONTRACT.md) — canonical public interface consumed by Hermes
- [Export and Sync Contract](docs/EXPORT_AND_SYNC_CONTRACT.md) — file write ownership and paths
- [Integration Principles](docs/INTEGRATION_PRINCIPLES.md) — boundary rules between engine and operations plane
- [Design Decisions](docs/DESIGN_DECISIONS.md) — why this is different from `ai-agent-test`
- [Implementation Plan](docs/IMPLEMENTATION_PLAN.md) — phase-by-phase build order
