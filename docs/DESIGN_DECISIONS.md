# Design Decisions
## jackhpark-pm-agentic-platform

This document explains the key design choices made in this project and how they relate to the reference implementation in `ai-agent-test/personal-poc/jack`.

---

## 1. Relationship to ai-agent-test

`ai-agent-test` is a well-engineered multi-agent system for USCIS civics learning. It was built as a learning exercise and contains several patterns worth studying. However, it is optimized for **conversational interaction**, not **traceable decision workflows**.

This project borrows its *design patterns* but not its code. The domain, execution model, and state design are all rebuilt from scratch.

---

## 2. Pattern Comparison

| Aspect | ai-agent-test | jackhpark-pm-agentic-platform | Reason for difference |
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
| **Scheduling** | APScheduler (daily/weekly coach) ✓ | Same pattern, PM signal collection schedule | Pattern reused directly |
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
- Approval gate state is managed by `WorkflowRun.status` in the database — no separate state machine framework needed
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

## 8. Patterns Directly Reused (by design, not by code copy)

These patterns from `ai-agent-test` are deliberately reproduced:

| Pattern | Source file | Applied as |
|---|---|---|
| Protocol-based storage | `agents/storage.py` | `app/storage/protocol.py` — same structural pattern |
| Factory function | `agents/factory.py` | `app/factory.py` — `build_engine(runtime)` |
| JSON event logging | `agents/logging.py` | `app/logging.py` — same emit pattern |
| Delivery abstraction | `agents/delivery.py` | Notification service protocol (future) |
| Async-first design | All agent `run()` methods | All stage functions are `async def` |
| Pydantic state schemas | `agents/state.py` | `app/models/stages.py` — stage I/O schemas |
