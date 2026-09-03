# Implementation Plan
## pm-intelligence-engine

**Principle:** Build a measurable foundation first, then stack features on it. The eval harness is created before any feature code so that every addition can be validated against real historical data.

---

## Workflow Overview

This platform automates a 7-stage PM decision workflow. Each stage is a discrete step that transforms the input it receives and writes its output to the database before passing control to the next stage.

| Stage | Name | What it does |
|-------|------|--------------|
| Stage 1 | Signal Ingestion | Normalizes and categorizes a raw external signal (article, STIG update, competitor move) |
| Stage 2 | Insight Extraction | Calls the LLM to extract the "what changed" insight and connect it to a product strategy pillar |
| Stage 3 | Opportunity Creation | Calls the LLM to frame a falsifiable hypothesis: "If we do X, then Y will happen because Z" |
| Stage 4 | Persona Evaluation | Runs 4 independent LLM agents in parallel, each evaluating the opportunity through a different lens |
| Stage 5 | Prioritization and Routing | Computes a composite score from the 4 persona scores and routes to PRD, PoC, or Kill |
| Stage 6A | PoC Plan | Designs a minimum experiment to validate the blocking assumptions identified in Stage 5 |
| Stage 6B | PRD | Generates a full Product Requirements Document for opportunities that cleared all blocking assumptions |
| Stage 7 | Executive Summary | Synthesizes all prior stage outputs into a 4-section narrative readable by any stakeholder |

The 4 personas used in Stage 4:

| Persona | Scoring Dimension | Weight | Question they answer |
|---------|-------------------|--------|---------------------|
| Explorer | Impact | 35% | "How far can we go with this?" |
| Strategist | Strategic Fit | 30% | "Does this belong in our direction?" |
| Builder | Feasibility | 20% | "Can we actually make this?" |
| Skeptic | Confidence | 15% | "What if we're wrong about this?" |

The composite score formula:
```
Composite = (Impact × 0.35) + (Strategic Fit × 0.30) + (Feasibility × 0.20) + (Confidence × 0.15)
```

Routing rules (applied deterministically by code, not the LLM):
- Any blocking assumption exists → Kill
- Composite ≤ 1.5 → Kill
- Confidence score ≥ 4 and no blocking assumptions → PRD
- All other cases → PoC

---

## Human-in-the-Loop Design

Not every signal needs a full 7-stage pipeline. Some signals are noise. Some are interesting but not actionable today. Running Stage 4 and Stage 5 on every signal wastes tokens and PM attention.

The platform solves this with two human gates and five configurable run modes.

### Run Modes

Every run produces a depth recommendation after Stage 2 based on the signal's relevance and urgency. The PM confirms or overrides before the rest of the pipeline runs.

| Mode | Stages run | When to choose |
|------|-----------|----------------|
| `file` | Stage 1 only | Signal is noise — wrong product, wrong segment, no action possible |
| `brief` | Stage 1 + 2 + 7 | Signal is interesting but low urgency — worth noting, not pursuing now |
| `opportunity` | Stage 1 + 2 + 3 | Signal warrants opportunity framing, but the team should decide before evaluating |
| `evaluate` | Stage 1 + 2 + 3 + 4 | Clearly relevant — run full persona evaluation, but not committing to a path yet |
| `decide` | Stage 1 + 2 + 3 + 4 + [gate] + 5 + 6 + 7 | Directly actionable — commit to evaluation, routing, and a next step |

### Gate 1 — Direction Gate (after Stage 2)

Stage 2 always runs immediately after Stage 1. After Stage 2 completes, the run pauses at `awaiting_direction` and the Stage 2 output includes a `suggested_mode` and `suggestion_reasoning`.

The PM either:
- Confirms the suggestion: `POST /runs/{id}/direction { "mode": "evaluate" }`
- Overrides it: `POST /runs/{id}/direction { "mode": "decide" }`

If a mode is passed upfront in `POST /runs/start { "mode": "decide" }`, the direction gate is skipped entirely.

### Gate 2 — Evaluation Gate (decide mode only, after Stage 4)

In `decide` mode, after Stage 4 completes, the run pauses at `waiting_approval`. The PM reviews the 4-persona evaluation and can:
- **Approve** — accept the evaluation and proceed to Stage 5 (Prioritization and Routing)
- **Revise** — provide written feedback; Stage 4 re-runs with feedback injected into every agent prompt, version number incremented
- **Reject** — kill the run with a recorded reason

### Gate 3 — Routing Review Gate (decide mode only, when Stage 5 routes to kill)

Stage 5's kill decision is based on the LLM's classification of assumptions as Blocking. This classification can be wrong — the LLM may mark an assumption as Blocking when an alternative path exists in the persona arguments. A silent, incorrect kill is the most damaging outcome.

When Stage 5 routes to kill, the run pauses at `waiting_routing_review`. The PM sees the composite score, blocking assumption list, and rationale, then can:
- **Confirm** — agree with the kill decision; run moves to `killed`
- **Override** — disagree with the blocking classification; choose `poc` or `prd` to proceed to Stage 6

For prd and poc routing, no gate is needed — Stage 4 approval already implied a decision to proceed, and prd/poc just determines which Stage 6 runs.

### State Machine

```
pending
  └─► running (Stage 1 + Stage 2)
        └─► awaiting_direction             ← Gate 1: PM confirms or overrides mode
              └─► running
                    ├─► completed           (mode: file, brief, opportunity, evaluate)
                    └─► waiting_approval    ← Gate 2: decide mode only, after Stage 4
                          ├─► running → waiting_routing_review  (approve → Stage 5 → kill)
                          │               └─► Gate 3: PM confirms or overrides kill
                          │                     ├─► killed    (confirm)
                          │                     └─► running → completed  (override to poc/prd)
                          ├─► running → completed  (approve → Stage 5 → prd/poc → Stage 6–7)
                          ├─► running → waiting_approval  (revise → Stage 4 retry)
                          └─► killed    (reject)
```

---

## Phase 0 — Project Setup and Documentation ✅
*Goal: Define everything before writing code. Reviewable, correctable, durable.*

| # | Task | Output |
|---|------|--------|
| 1 | Create project directory | `code/core/pm-intelligence-engine/` |
| 2 | README.md | Overview, architecture diagram, setup |
| 3 | docs/PRD.md | Feature catalog (F1–F13), acceptance criteria |
| 4 | docs/ARCHITECTURE.md | Layer design, data model, component map |
| 5 | docs/DESIGN_DECISIONS.md | Key architectural choices and the reasoning behind them |
| 6 | docs/IMPLEMENTATION_PLAN.md | This document |
| 7 | CLAUDE.md | Project context for AI-assisted development |
| 8 | pyproject.toml | Dependency declarations |

---

## Phase 1 — Foundation + Core Loop
*Goal: `POST /signals` → Stage 1 through Stage 3 run automatically → result saved to DB → eval passes*

### 1.1 Project skeleton
- `config.py` — `DECISION_SYSTEM_ROOT`, `WIKI_ROOT`, `LLM_PROVIDER`
- `app/storage/protocol.py` — `PMWorkflowStore` Protocol (interface definition)
- `app/storage/sqlite_store.py` — SQLite implementation with all 5 tables
- `app/factory.py` — `build_engine(runtime="local" | "production")`
- `app/logging.py` — `emit_event(stage, action, run_id, detail)` → structured JSON to stdout

**Why first:** Every other component depends on storage and config. Defining the protocol before the implementation forces a clean interface.

**Acceptance criteria:**
- `build_engine("local")` returns a wired engine with SQLite store
- `emit_event()` writes valid JSON to stdout

### 1.2 LLM provider abstraction
- `app/llm/protocol.py` — `LLMProvider` Protocol and `Message` TypedDict
- `app/llm/claude.py` — Anthropic Claude implementation
- `app/llm/openai.py` — OpenAI implementation
- `factory.py` reads `LLM_PROVIDER` env var to select provider at startup

**Why here:** All stage functions depend on `LLMProvider`. Define the interface before writing stages.

**Acceptance criteria:**
- Both providers pass the same unit test: given identical messages, both return a non-empty string
- Swapping `LLM_PROVIDER=openai` in `.env` changes which provider is used, with no other code changes

### 1.3 Context loader and template service
- `app/services/context_loader.py`
  - `load_pm_identity()` → reads `core/00-pm-identity.md` from `DECISION_SYSTEM_ROOT`
  - `load_company_context()` → reads `company-context.md`
  - `load_product_context(product_id)` → reads `products/<name>/context.md`
  - `load_full_context(product_id)` → merges all three into a single `FullContext` object
- `app/services/template_service.py`
  - `load_template(stage)` → reads prompt template file from `prompts/`
  - `render_template(template, variables)` → string substitution with validation

**Acceptance criteria:**
- `load_full_context("example-security-product")` returns all three layers as a structured object
- `render_template()` raises `ValueError` if a required variable is missing from the template

### 1.4 Stage schemas (Stage 1 through Stage 3)
- `app/models/stages.py` — Pydantic models for each stage's input and output:
  - `S1Input`, `S1Output` — signal title, summary, category, source
  - `S2Input`, `S2Output` — insight, "what changed", pillar reference
  - `S3Input`, `S3Output` — problem statement, target user, falsifiable hypothesis

**Acceptance criteria:**
- All models have field-level descriptions
- Invalid inputs raise Pydantic `ValidationError`, not generic exceptions

### 1.5 Stage functions: Stage 1 through Stage 3
- `app/stages/s1_signal.py` — No LLM call; normalizes and categorizes the raw signal text
- `app/stages/s2_insight.py` — LLM call; extracts "what changed" and connects it to a product strategy pillar
- `app/stages/s3_opportunity.py` — LLM call; frames a falsifiable hypothesis from the insight

Every stage follows the same function signature:
```python
async def run(
    input: StageInput,
    context: RunContext,
    llm: LLMProvider,
    store: PMWorkflowStore,
) -> StageOutput
```

**Acceptance criteria:**
- Each stage saves its output to `store.save_stage_output()` before returning
- Stage 2 output references at least one strategy pillar from the product context
- Stage 3 hypothesis is non-empty and contains a testable claim

### 1.6 Signal API and Run API
- `app/api/signals.py` — `POST /signals`, `GET /signals`
- `app/api/runs.py` — `POST /runs/start`, `GET /runs/{id}`
- `app/api/main.py` — FastAPI app wiring all routers together

**Acceptance criteria:**
- `POST /signals` creates a database record and returns a `signal_id`
- `POST /runs/start` triggers Stage 1 through Stage 4 asynchronously and returns a `run_id` immediately
- `GET /runs/{id}` returns current status and available stage outputs

### 1.7 Eval harness skeleton
- `eval/scenarios.json` — 2 golden scenarios: R04 (expected routing: PRD), R06 (expected routing: Kill)
- `eval/runner.py` — Loads scenarios, runs Stage 1 through Stage 3, verifies outputs are valid Pydantic models
- `eval/rubrics/s3_hypothesis.py` — Checks Stage 3 output for hypothesis falsifiability

**Why here, before Stage 4 through Stage 7:** Building the harness now means every subsequent phase can be validated immediately. Routing accuracy will be added in Phase 2 once Stage 5 exists.

**Acceptance criteria:**
- `python eval/runner.py` runs without error and outputs a pass/fail summary
- Both scenarios produce valid Stage 1 through Stage 3 outputs

### Phase 1 completion gate
> Signal submitted via API → Stage 1 through Stage 3 execute automatically → outputs saved to DB → eval harness passes for both scenarios

---

## Phase 2 — The Core Judgment Loop
*Goal: Full Stage 1 through Stage 7 run with PM approval gate; routing verified against golden dataset*

### 2.1 Stage 4: Persona Evaluation — 4 independent LLM agents

Stage 4 is the core multi-agent step. Four LLM agents each receive the same opportunity and evaluate it independently, from their own fixed perspective. They cannot see each other's responses — this enforces independence and mirrors a real PM review panel.

- `app/agents/explorer.py` — Impact lens: "How far can we go with this?"
- `app/agents/strategist.py` — Strategic Fit lens: "Does this belong in our direction?"
- `app/agents/builder.py` — Feasibility lens: "Can we actually make this?"
- `app/agents/skeptic.py` — Confidence lens: "What if we're wrong about this?"
- `app/agents/base.py` — Shared base class: `evaluate(opportunity, context, llm) → PersonaOutput`

Each agent returns:
- A score from 1 to 5 for its dimension
- A 2–4 sentence argument grounded in the product context
- One open question naming who can answer it and how

Orchestrator:
- `app/stages/s4_evaluation.py` — uses `asyncio.gather()` to run all 4 agents in parallel; saves 4 independent outputs to the database with no synthesis

Quality checker:
- `eval/rubrics/s4_rubric.py` — 12-point rubric (3 points each):
  1. Score Grounding: all 4 agents reference specific product context elements
  2. Skeptic Quality: the counter-argument is a concrete steelman, not "insufficient data"
  3. Open Question Quality: all questions name who answers them and how
  4. Persona Independence: the Skeptic's score differs from the others

**Why parallel:** Each persona must not see the others' answers before forming their own. Parallel execution enforces independence while also reducing total runtime.

**Acceptance criteria:**
- 4 outputs saved independently with no cross-contamination
- Rubric score ≥ 9/12 for the run to be considered high quality
- Total Stage 4 execution time under 60 seconds

### 2.2 Human-in-the-Loop API

Two APIs implement the two human gates described in the HIL design above.

**Gate 1 — Direction Gate** (`app/api/direction.py`):
- `POST /runs/{id}/direction { mode: string }` — confirms or overrides the Stage 2 mode recommendation
  - Validates mode is one of: `file`, `brief`, `opportunity`, `evaluate`, `decide`
  - Transitions status: `awaiting_direction` → `running`
  - Triggers the stages appropriate for the chosen mode

The `POST /runs/start` request also accepts an optional `mode` field. If provided, the direction gate is skipped and the run proceeds immediately with the specified mode after Stage 2 completes.

**Gate 2 — Evaluation Gate** (`app/api/approvals.py`, `decide` mode only):
- `POST /runs/{id}/approve` → transitions `waiting_approval` → `running`, triggers Stage 5
- `POST /runs/{id}/revise { feedback: string }` → re-runs Stage 4 with PM feedback injected into all agent prompts, increments output version number
- `POST /runs/{id}/reject { reason: string }` → moves run to `killed`, records reason

All approval endpoints return 409 if the run is not in `decide` mode.

**Gate 3 — Routing Review Gate** (`app/api/routing_review.py`, fires only when Stage 5 routes to kill):
- `POST /runs/{id}/routing-review { action: "confirm" }` → confirms kill; run moves to `killed`
- `POST /runs/{id}/routing-review { action: "override", routing: "poc" | "prd" }` → PM overrides the blocking classification; Stage 6 runs with the chosen routing

This gate exists because Stage 5's kill decision is driven by LLM assumption classification, which can misclassify an assumption as Blocking when an alternative path exists. A silent incorrect kill is the most costly outcome.

**Acceptance criteria:**
- Direction gate transitions: `awaiting_direction` → `running`, then appropriate stages execute
- Approval gate transitions: `waiting_approval` → `running` on approve; `killed` on reject
- Stage 5 kill routing → `waiting_routing_review`; prd/poc routing → Stage 6 immediately
- Routing review confirm → `killed`; override → Stage 6 with chosen routing
- Revise saves a new Stage 4 output with an incremented version number alongside the original
- Approval endpoints return 409 for non-`decide` mode runs

### 2.3 Stage 5: Prioritization and Routing

Stage 5 takes the 4 persona scores from Stage 4 and makes the routing decision. The composite score calculation and routing rule are deterministic Python code — they are never delegated to the LLM.

The LLM's role in Stage 5 is limited to: classifying the open assumptions from Stage 4 as Blocking or Informing, and writing a rationale sentence.

- `app/stages/s5_prioritization.py`
  - Reads the 4 persona scores from Stage 4 output
  - Computes composite score: `Impact × 0.35 + Strategic Fit × 0.30 + Feasibility × 0.20 + Confidence × 0.15`
  - Calls LLM to classify assumptions as Blocking (kills the opportunity if false) or Informing (narrows scope if false)
  - Applies routing rule in Python code:
    - Any blocking assumption → Kill
    - Composite ≤ 1.5 → Kill
    - Confidence score ≥ 4 and no blocking assumptions → PRD
    - Otherwise → PoC
- `eval/rubrics/s5_routing.py` — routing accuracy checker for the eval harness

**Acceptance criteria:**
- Composite score and routing stored on the `WorkflowRun` record
- Kill routing terminates the run immediately (Stage 6 and Stage 7 do not execute)
- Eval scenarios R04, R05, R07 route to PRD; R06 routes to Kill

### 2.4 Stage 6: PoC Plan (6A) or PRD (6B)

Only one of these stages runs, depending on the routing decision from Stage 5.

**Stage 6A — PoC Plan** (runs when routing = `poc`):
- `app/stages/s6a_poc_plan.py`
- Output: minimum experiment design targeting the blocking assumptions from Stage 5
- Includes: experiment goal, actions per assumption, success/failure criteria, timeline in weeks, resources needed
- Rule: the experiment must not require engineering work before assumptions are validated

**Stage 6B — PRD** (runs when routing = `prd`):
- `app/stages/s6b_prd.py`
- Output: full Product Requirements Document ready for an engineering team
- Includes: problem statement, target user, user stories (≥3), success metrics (≥2), in-scope list, out-of-scope list (≥2 items), technical dependencies, open questions with suggested owners, risks
- Completeness check: 12-point boolean checklist computed automatically from the LLM output

**Acceptance criteria:**
- The correct stage executes based on Stage 5 routing
- Stage 6B PRD completeness checklist is computed and stored with the output

### 2.5 Stage 7: Executive Summary

Stage 7 synthesizes all prior stage outputs into a narrative that a stakeholder who was not part of the run can read and fully understand. It introduces no new information — every claim must reference a prior stage output.

- `app/stages/s7_summary.py`
- Loads Stage 1 through Stage 6 outputs from the database
- Calls the LLM to write a 4-section summary:
  1. **What we saw** — factual signal summary (Stage 1 and Stage 2)
  2. **What it means** — insight and product implications (Stage 3)
  3. **What we decided** — composite score, routing, key assumptions (Stage 4 and Stage 5)
  4. **What we will do next** — next steps in plain language (Stage 6)
- Output saved as both a structured JSON `StageOutput` and a Markdown `Artifact` record in the database

**Acceptance criteria:**
- Stage 7 output stored as both Markdown and JSON artifact
- Run status transitions to `completed` after Stage 7

### 2.6 Run file export

After a run completes, the platform can export all stage outputs as Markdown files into the canonical pm-engine archive. Historical manual files may still exist in decision-context, but they are not the active export target.

- `app/services/run_exporter.py`
  - Creates directory: `archive/runs/<product_id>/<YYYY-MM-DD>-<slug>/`
  - Writes one file per stage: `s1-signal.md` through `s7-report.md`
  - Format matches the existing manual run files exactly

**Acceptance criteria:**
- Exported files are readable alongside manually-created run files without visible difference
- Existing run files in `DECISION_SYSTEM_ROOT` are never modified

### Phase 2 completion gate
> Signal submitted → Stage 1 + 2 run → run pauses at direction gate with a mode recommendation → PM confirms `decide` mode → Stage 3 + 4 run → run pauses at evaluation gate → PM approves → Stage 5 routing is correct → PRD or PoC Plan generated → Executive Summary written → files exported to `archive/runs/<product_id>/<YYYY-MM-DD>-<slug>/`.
> Eval harness: R04/R05/R07 route to PRD, R06 routes to Kill.

---

## Phase 3 — Operational Automation
*Goal: History is queryable; completion side-effects are uniform; external integrations are contracted*

> **Ownership update (2026-05):** Signal harvesting (RSS, file watch, APScheduler) and
> wiki sync have been moved to the **external operations plane** (separate repository).
> pm-engine is the execution engine; the operations plane is the operational host.
> See `docs/INTEGRATION_PRINCIPLES.md` and `docs/EXPORT_AND_SYNC_CONTRACT.md`.

### 3.1 Automated signal collection
**→ Moved to the external operations plane.**
It harvests signals (RSS/file-watch) and submits them via `POST /signals`.
pm-engine's role: accept the API call, deduplicate by URL hash if needed.

### 3.2 Signal and run history queries
- `GET /signals?product_id=&status=&limit=` — full filter support
- `GET /runs?product_id=&routing=&status=&limit=` — run history with filters

**Acceptance criteria:**
- Correct filtering by all supported query parameters
- Results are sorted by `ingested_at` / `created_at` descending

### 3.3 Completion side-effect unification ✅
All terminal run transitions now go through `app/services/run_finalizer.py`:
- `completed_at` auto-stamped for `completed` / `killed` (not `failed`)
- decision-system export triggered for `decide`-mode completions
- uniform event emission for ops-plane polling

### 3.4 Wiki sync contract
**→ Ops-plane-owned.** `app/services/wiki_sync.py` is a utility adapter that documents
canonical paths (`raw/from-pm-decision-context/{prds|poc-upgrades|kills}/`).
pm-engine completion paths do NOT write to WIKI_ROOT.

### Phase 3 completion gate
> Run history queryable via API → completion side-effects uniform via run_finalizer →
> Ops-plane integration contract documented → decision-system export wired

---

## Testing Strategy

### Unit tests (`tests/unit/`)
- One test file per stage: `test_s1.py` through `test_s7.py`
- LLM is replaced with a mock that returns fixture JSON strings
- Storage is replaced with a mock that records method calls
- Each test verifies: valid input → valid Pydantic output; invalid input → `ValidationError`

### Integration tests (`tests/integration/`)
- `test_full_run.py` — Stage 1 through Stage 7 against a real SQLite database, with mock LLM
- `test_approval_flow.py` — approve, revise, and reject status transitions
- `test_context_loader.py` — reads from the real `DECISION_SYSTEM_ROOT` on disk

### Eval harness (`eval/`)
- `eval/runner.py` — runs the full Stage 1 through Stage 5 pipeline against 4 golden scenarios
- `eval/scenarios.json` — R04 (PRD), R05 (PRD), R06 (Kill), R07 (PRD)
- Routing accuracy must be 100% before any phase is considered complete

---

## Dependencies (pyproject.toml)

```toml
[project]
name = "pm-intelligence-engine"
version = "0.1.0"
requires-python = ">=3.12"

dependencies = [
    "fastapi[standard]>=0.115.0",
    "sqlalchemy>=2.0.0",
    "pydantic>=2.0.0",
    "pydantic-settings>=2.0.0",
    "apscheduler>=3.10.0",
    "httpx>=0.27.0",           # RSS fetch, async HTTP
    "feedparser>=6.0.0",       # RSS parsing
    "python-dotenv>=1.0.0",
    "pyyaml>=6.0.0",           # frontmatter generation
]

[project.optional-dependencies]
anthropic = ["anthropic>=0.40.0"]
openai = ["openai>=1.60.0"]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.23.0",
    "ruff>=0.4.0",
    "mypy>=1.10.0",
]
```
