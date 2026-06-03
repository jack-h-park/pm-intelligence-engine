# CLAUDE.md — jackhpark-pm-intelligence-engine *(→ pm-intelligence-engine)*

This file provides context for AI-assisted development on this project.

---

## What This Project Is

A personal PM intelligence platform that automates the signal-to-decision workflow. It reads from two existing repositories and writes results back to them.

**This project does NOT contain the workflow design.** The workflow (prompts, product contexts, PM identity, scoring rules) lives in `jackhpark-pm-decision-context`. This project automates that workflow.

---

## Key External Repositories

Configured in `config.py`:

```python
DECISION_CONTEXT_ROOT = "/Users/jackpark/workspace/ai-assets/jackhpark-pm-decision-context"
WIKI_ROOT = "/Users/jackpark/workspace/ai-assets/jackhpark-product-management-wiki"
```

**jackhpark-pm-decision-context** — source of:
- `core/00-pm-identity.md` — PM philosophy and operating principles (loaded into every LLM call)
- `company-context.md` — company strategy context
- `products/<name>/context.md` — product-specific context
- `products/<name>/signal-sources.md` — RSS/URL sources for signal collection
- `prompts/s1/` through `prompts/s7/` — stage prompt templates
- `products/<name>/runs/` — where completed run files are written

**jackhpark-product-management-wiki** — used by Hermes (not pm-engine directly):
- `raw/from-web/sensing/` — Hermes watches for new signal files
- `raw/from-pm-decision-context/` — Hermes writes wiki sync output after pm-engine completion

**pm-engine does not write to WIKI_ROOT from completion paths.** Wiki sync is Hermes-owned.
See `docs/EXPORT_AND_SYNC_CONTRACT.md` for the full ownership table.

---

## Project Structure

```
app/
├── models/
│   ├── workflow.py       SQLAlchemy ORM models (5 tables)
│   └── stages.py         Pydantic I/O schemas for each stage
├── stages/               One async function per stage: s1_signal.py → s7_summary.py
├── agents/               Stage 4 persona agents: explorer, strategist, builder, skeptic
├── services/
│   ├── context_loader.py  Reads 3-layer context from DECISION_CONTEXT_ROOT / DECISION_SYSTEM_ROOT
│   ├── template_service.py Loads and renders prompt templates
│   ├── run_finalizer.py   Single exit point for terminal transitions; triggers export
│   ├── run_exporter.py    Writes completed runs to DECISION_CONTEXT_ROOT / DECISION_SYSTEM_ROOT format
│   ├── notifier.py        FanoutNotifier: Gate 1 + Gate 2 Telegram/Slack alerts
│   └── wiki_sync.py       Utility adapter only — NOT called from completion paths
├── storage/
│   ├── protocol.py        PMWorkflowStore Protocol (interface)
│   └── sqlite_store.py    SQLite implementation
├── llm/
│   ├── protocol.py        LLMProvider Protocol
│   ├── claude.py          Anthropic implementation
│   └── openai.py          OpenAI implementation
├── api/
│   ├── main.py            FastAPI app
│   ├── signals.py         /signals routes
│   ├── runs.py            /runs routes + Gate 1 logic
│   ├── direction.py       /runs/{id}/direction — Gate 1 response
│   ├── approvals.py       /runs/{id}/approve|revise|reject — Gate 2
│   ├── routing_review.py  /runs/{id}/routing-review — Gate 3
│   ├── artifacts.py       /runs/{id}/artifacts — artifact query
│   └── review.py          /runs/{id}/review — browser Gate 2 review page
├── factory.py             build_engine(runtime) — dependency wiring
└── logging.py             emit_event() — structured JSON logging

eval/
├── scenarios.json        Golden dataset (R01–R07 historical runs)
├── runner.py             Full scenario regression runner
└── rubrics/              Stage-specific quality evaluators

tests/
├── unit/                 Per-stage unit tests with mocked LLM and store
└── integration/          Full flow tests against SQLite + real file system
```

---

## Architecture Summary

### Workflow stages (sequential)
```
Stage 1: Signal Ingestion
  → Stage 2: Insight Extraction
  → Stage 3: Opportunity Creation
  → Stage 4: Persona Evaluation [Explorer, Strategist, Builder, Skeptic run in parallel]
  → [PM approval gate]
  → Stage 5: Prioritization and Routing (prd / poc / kill)
  → Stage 6A: PoC Plan  (if routing = poc)
     OR
     Stage 6B: PRD       (if routing = prd)
  → Stage 7: Executive Summary
```

### State machine (WorkflowRun.status)
```
pending → running
  → [auto-triage]         → completed (mode=file)
  → awaiting_direction    ← Gate 1: POST /runs/{id}/direction
  → running (mode set)
  → [non-decide]          → completed
  → waiting_approval      ← Gate 2: approve / revise / reject
      [approve]  → running → S5 → ...
      [revise]   → running (S4 re-runs with PM feedback)
      [reject]   → killed
  → [S5 routes kill] → waiting_routing_review
      [confirm]  → killed
      [override] → running → S6 → S7 → completed
  → [S5 routes prd/poc] → S6 → S7 → completed
```

Terminal states: `completed` and `killed` receive `completed_at`; `failed` does not.

### LLM abstraction
All LLM calls go through `LLMProvider`. No stage imports an SDK directly.
Set `LLM_PROVIDER=claude` or `LLM_PROVIDER=openai` in `.env`.

---

## Development Rules

1. **All documentation in English.** Comments, docstrings, and docs files are English only.
2. **Stages have typed contracts.** Every stage function signature:
   ```python
   async def run(input: StageInput, context: RunContext, llm: LLMProvider, store: PMWorkflowStore) -> StageOutput
   ```
3. **No LLM calls outside stage functions or agent evaluate() methods.**
4. **No direct SDK imports in stage files.** Use `LLMProvider` only.
5. **Eval must pass before a phase is considered complete.** Run `python eval/runner.py` before marking any phase done.
6. **Storage writes happen inside stages, not in API routes.**
7. **Run files exported to `DECISION_SYSTEM_ROOT` must match the existing manual format exactly.**
8. **pm-engine does not write to WIKI_ROOT from completion paths.** Wiki sync is Hermes-owned.

---

## Running the Project

```bash
# Install
pip install -e ".[dev,anthropic]"

# Environment
cp .env.example .env
# Set: LLM_PROVIDER, ANTHROPIC_API_KEY or OPENAI_API_KEY

# Start API
uvicorn app.api.main:app --reload

# Run eval harness
python eval/runner.py

# Run tests
pytest tests/unit/
pytest tests/integration/ -m "not slow"
```

---

## Key Data Contracts

### Stage output structure (all stages)
```json
{
  "stage": "s3",
  "run_id": "uuid",
  "version": 1,
  "output": { ... stage-specific fields ... },
  "metadata": { "created_at": "...", "model_used": "..." }
}
```

### Approval event
```json
{
  "run_id": "uuid",
  "stage": "s4",
  "action": "approve | revise | reject",
  "feedback_text": "optional string",
  "created_at": "ISO 8601"
}
```

### WorkflowRun routing values
- `"prd"` — proceed to Stage 6B: PRD generation
- `"poc"` — proceed to Stage 6A: PoC Plan generation
- `"kill"` — terminate run, record reason
- `null` — routing not yet determined (Stage 5 has not run yet)

---

## Eval Harness Notes

**Golden dataset location:** `eval/scenarios.json`

**Expected routing (from historical manual runs):**

| Scenario ID | Signal | Expected routing |
|-------------|--------|-----------------|
| R04 | Android 16 Advanced Protection Mode — no admin enforcement API | `prd` |
| R05 | Android 16 native NFC admin control | `prd` |
| R06 | DISA Android 16 STIG mandates dedicated MTD app | `kill` |
| R07 | Android 16 RKP attestation transition | `kill` (Gate 3 override → `prd`) |

**To add a new scenario to `eval/scenarios.json`:**
```json
{
  "run_id": "R08",
  "product_id": "samsung-knox-lockdown-mode",
  "title": "Short descriptive title for the signal",
  "signal_text": "Full signal text as it would be submitted via POST /signals",
  "expected_routing": "prd",
  "expected_composite_range": [3.5, 5.0],
  "notes": "Why this scenario was chosen and what it validates"
}
```
