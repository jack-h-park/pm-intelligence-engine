# Design Decisions
## pm-intelligence-engine

This document explains the key design choices made in this project and how they relate to the reference implementation in `ai-agent-test/personal-poc/jack`.

---

## 1. Relationship to ai-agent-test

`ai-agent-test` is a well-engineered multi-agent system for USCIS civics learning. It was built as a learning exercise and contains several patterns worth studying. However, it is optimized for **conversational interaction**, not **traceable decision workflows**.

This project borrows its *design patterns* but not its code. The domain, execution model, and state design are all rebuilt from scratch.

---

## 2. Pattern Comparison

| Aspect | ai-agent-test | pm-intelligence-engine | Reason for difference |
|---|---|---|---|
| **Orchestration model** | Keyword-based intent routing → agent dispatch | Sequential stage functions with state machine + approval gate | PM work requires traceable, step-by-step decision records, not conversational routing |
| **State model** | Session-centric (`OrchestratorState` with message history) | Run-centric (`WorkflowRun` with stage outputs and approval events) | The unit of work is a decision run, not a chat session |
| **Domain units** | 7 role-based agents (quiz, tutor, interview, …) | 7 stage functions (S1–S7) each with typed I/O | Stages have deterministic input/output contracts; conversational agents are open-ended |
| **Storage abstraction** | `LearningStore` Protocol ✓ | `PMWorkflowStore` Protocol (same pattern, different schema) | Pattern reused directly — clean abstraction makes SQLite → PostgreSQL trivial |
| **Factory / DI** | `build_orchestrator(runtime)` ✓ | `build_engine(runtime)` (same pattern) | Pattern reused directly — separates local (in-memory/SQLite) from production wiring |
| **LLM layer** | Hardwired to LangChain + OpenAI | `LLMProvider` Protocol (vendor-agnostic) | Avoid vendor lock-in; prompt style is independent of SDK |
| **Logging** | `emit_agent_event()` JSON logger ✓ | Same pattern, PM-specific event taxonomy | Structured JSON log is essential for PM judgment audit trail |
| **MCP servers** | Core architectural component | Optional, Phase 3+ only | MCP adds integration surface area not needed for PoC; adds upfront complexity with little v1 return |
| **RAG** | Required (civics knowledge base in Qdrant) | Optional, Phase 4+ (wiki semantic search) | Domain knowledge is loaded directly from files, not vectors; file-based loading is sufficient for v1 |
| **Human-in-the-loop** | Not present | First-class design (approval gate after S4) | PM judgment must not be bypassed by automation; the gate is a core PM workflow requirement |
| **Eval harness** | Not present | Built before any feature code (Phase 0) | Quality measurement from day one using real historical runs as golden data |
| **Scheduling** | APScheduler (daily/weekly coach) ✓ | Externalized to the operations plane | Scheduling exists as a system capability, but not as an in-process engine concern |
| **Delivery abstraction** | `DeliveryService` Protocol ✓ | `NotificationService` Protocol (same pattern) | Makes console → Slack → email swap trivial |

---

## 3. Why Stage Functions Instead of Agents

In `ai-agent-test`, each "agent" is a class with a `run()` method that owns its own domain logic. This is appropriate for open-ended conversational tasks where the agent might ask clarifying questions or produce diverse outputs.

In this project, each "stage" is a function with a strict signature:
```python
async def run(input: StageInput, context: RunContext, llm: LLMProvider) -> StageOutput
```

**Why this matters:**
- Every stage has a testable, typed contract — easy to mock in unit tests
- Stage outputs are always structured (Pydantic models), never free-form text
- Stages can be re-run individually if a revision is requested
- The eval harness can check stage outputs against expected schemas

The trade-off: stages are less flexible than agents. But for PM decision workflows, rigidity is a feature — it prevents the system from producing unchecked narrative outputs that can't be traced or evaluated.

---

## 4. Why One Approval Gate (Not Three)

The original plan considered multiple approval gates (after S3, after S4, after S7). This was simplified to one gate after S4.

**Reasoning:**
- S1–S3 are relatively deterministic (signal normalization, insight extraction, opportunity framing). PM review at this stage has low value and creates friction.
- S4 is where the most judgment-sensitive content is produced (four independent persona evaluations). This is the right moment for PM oversight before the scoring and routing decision locks in.
- S7 (Executive Summary) is a synthesis of already-approved content. A second gate here would slow the workflow without adding meaningful control.

**Trade-off:** If S5 routing is wrong (e.g., should be Kill but routes to PRD), the PM only catches it when reviewing the PRD or Executive Summary. This is acceptable for v1 — the PM can reject the artifact and the run record will reflect the discrepancy.

A second gate can be added before S7 in a future version if the need is demonstrated.

---

## 5. Why SQLite First (Not PostgreSQL)

`ai-agent-test` uses PostgreSQL with Alembic migrations from the start. This is appropriate for a system intended to run in a multi-user production environment.

This project uses SQLite in v1 because:
- It is a single-user personal tool
- No deployment infrastructure required
- SQLite is sufficient for hundreds of runs
- The `PMWorkflowStore` Protocol abstraction makes swapping to PostgreSQL a one-file change

The schema design mirrors what PostgreSQL-ready code would look like (proper UUIDs, foreign keys, enum columns). The migration to PostgreSQL in v2 should be mechanical, not architectural.

---

## 6. Why No LangGraph in v1

`ai-agent-test` optionally uses LangGraph for its `StateGraph` orchestration. This is appropriate for complex, branching workflows with durable state.

This project does not use LangGraph in v1 because:
- The workflow has only one branching point (S5 routing: PRD / PoC / Kill) — this is a simple `if` statement
- Approval gate state is managed by `WorkflowRun` `lifecycle`+`position` in the database (US-55; formerly a single `status` enum) — no separate state machine framework needed
- LangGraph adds a learning curve and abstraction layer that slows PoC iteration
- The upgrade path is clear: if branching becomes complex (multiple gates, retries, parallel tracks), LangGraph can replace the plain `if` logic without changing stage functions

---

## 7. Why Vendor-Agnostic LLM

`ai-agent-test` uses LangChain, which provides a unified LLM interface but adds significant abstraction overhead.

This project uses a minimal `LLMProvider` protocol with direct SDK calls:
```python
class LLMProvider(Protocol):
    async def complete(self, messages: list[Message], model: str | None = None) -> str: ...
```

**Reasons:**
- No LangChain dependency = fewer layers of indirection to debug
- Protocol is simple enough to implement for any provider in ~30 lines
- Model selection per stage is possible (e.g., use a cheaper model for S1, a stronger model for S4)
- Prompt templates are plain Markdown files, not LangChain PromptTemplate objects — easier to read and edit

---

## 8. S5 Routing Rule: Two-Axis Hybrid (2026-06-10)

The deterministic routing rule in `_compute_routing()` (`app/stages/s5_prioritization.py`)
has gone through three versions:

| Version | Rule | Problem |
|---|---|---|
| v1 (initial commit) | `skeptic_score >= 4 → prd`, else `poc` — faithful to `04-scoring.md`'s Confidence-based routing | Routing flips on a single persona's one-point swing (Skeptic 3 vs 4); brittle against LLM score variance |
| v2 (`c124036`, 2026-05-23) | `composite >= 3.5 → prd`, else `poc` | Confidence is diluted to its 0.15 weight. A high-impact, unvalidated opportunity (e.g. 5/5/4/2 → composite 4.35) skips PoC and goes straight to PRD — the opposite of the framework's stated intent ("low confidence is a signal to run a PoC"). The design doc was never updated, leaving a silent doc/code mismatch. |
| **v3 (decided 2026-06-10)** | **Two-axis hybrid** (see below) | — |

**v3 rule** (first match wins). *Revised 2026-07-03 (US-55): a Blocking assumption
routes to `poc`, not `kill` — Kill is reserved for a low composite (value floor).*

```
1. composite <= KILL_THRESHOLD (1.5)                          → kill  (low value)
2. any blocking assumption                                    → poc   (validate the unresolved question)
3. composite >= PRD_THRESHOLD (3.5) AND confidence >= 4       → prd
4. otherwise                                                  → poc
```

> Previously (before 2026-07-03) rule 1 was "blocking assumptions **OR** composite
> ≤ 1.5 → kill". The first production `decide` run showed a feasibility Blocker
> wrongly killing a composite-3.6 opportunity against S5's own PoC rationale; a
> Blocking assumption is an unresolved feasibility/dependency question (a PoC
> case), and a true value-nullifier already lands at/below the kill floor. See
> `core/04-scoring.md` in decision-context.

**Rationale:** composite and confidence answer different questions. Composite
("how good is this overall?") sets the quality floor — it decides kill and
PRD-eligibility, and its weighted-average stability prevents single-score flips
on the kill decision. Confidence ("do we know enough to commit?") is the
readiness gate — it alone decides PRD vs PoC for strong opportunities. This
restores v1's intent while keeping v2's stability where it matters.

**Residual brittleness is accepted:** PRD vs PoC can still flip on Skeptic
3 → 4, but Gate 3 (a run `paused` at `position=s5`) puts a human on every routing
decision, so the rule only needs to be a good default, not an infallible one.

**Thresholds** (kill 1.5, prd 3.5, confidence gate 4) move from hardcoded
constants to per-product `scoring.yaml`, following the existing pattern for
dimension weights.

Canonical framework documentation: `pm-decision-context/core/04-scoring.md`
("Routing Decision — Two-Axis Hybrid Rule"). That document and this code must
change together — the v2 episode is the cautionary example.

---

## 9. Patterns Directly Reused (by design, not by code copy)

These patterns from `ai-agent-test` are deliberately reproduced:

| Pattern | Source file | Applied as |
|---|---|---|
| Protocol-based storage | `agents/storage.py` | `app/storage/protocol.py` — same structural pattern |
| Factory function | `agents/factory.py` | `app/factory.py` — `build_engine(runtime)` |
| JSON event logging | `agents/logging.py` | `app/logging.py` — same emit pattern |
| Delivery abstraction | `agents/delivery.py` | Notification service protocol (future) |
| Async-first design | All agent `run()` methods | All stage functions are `async def` |
| Pydantic state schemas | `agents/state.py` | `app/models/stages.py` — stage I/O schemas |
