# Reverse Roadmap
## Gap Closure Backlog

**Version:** 0.1  
**Last updated:** 2026-05-24

This document contains only the remaining gaps from the reverse PRD. Implemented baseline behavior stays in [Reverse PRD](PRD_REVERSE.md); evidence stays in [Implementation Status](IMPLEMENTATION_STATUS.md).

---

## Prioritization Logic

The roadmap prioritizes work that:

1. closes product promises already implied by the current architecture
2. reduces the mismatch between implemented feature surface and verification depth
3. strengthens external-repository interoperability

---

## Now

### E4 — Knowledge Export and Sync

#### US-20 — Automatic executive summary sync
**Status:** Partially Implemented

As a PM, I want completed executive summaries synced into the wiki automatically so learning accumulates without manual copying.

**Why now**

- the product already generates Stage 7 outputs
- the sync function exists
- the main missing piece is orchestration and contract hardening

**Acceptance criteria**

- completion of a run that produces Stage 7 triggers wiki sync automatically
- routing maps deterministically to the correct wiki subdirectory
- sync failures are visible and do not silently discard the completion event
- path contract is aligned with one canonical wiki destination structure

**Dependencies**

- finalized output path convention
- completion hook placement in run lifecycle

### E5 — Operational Automation and Quality Safety

#### US-22 — Full workflow integration coverage
**Status:** Partially Implemented

As a PM, I want integration coverage for real workflow paths so state transitions and orchestration are validated end to end.

**Acceptance criteria**

- add integration coverage for a successful `decide` run through Stage 7 with mocked LLM
- add integration coverage for approve, revise, reject transitions
- add integration coverage for routing-review confirm and override paths
- tests assert persisted stage outputs, run status transitions, and artifact creation

**Dependencies**

- stable test harness around SQLite and background task execution

#### US-25 — Stable completion semantics
**Status:** Partially Implemented

As a PM, I want stable operational semantics for terminal statuses and completion side effects so completed runs are trustworthy.

**Acceptance criteria**

- terminal transitions set all expected run metadata consistently
- `completed_at` behavior is defined and enforced
- export/sync side effects occur from one explicit completion orchestration path
- kill, failed, and completed paths have distinct and documented side-effect behavior

---

## Next

### E5 — Operational Automation and Quality Safety

#### US-21b — Stage 6 and Stage 7 unit coverage
**Derived gap from US-21**

As a maintainer, I want unit coverage for S6A, S6B, and S7 so later prompt or schema changes do not break artifact generation silently.

**Acceptance criteria**

- add unit tests for S6A JSON parsing and output persistence
- add unit tests for S6B completeness scoring and output persistence
- add unit tests for S7 partial/full mode summary behavior and artifact save

### E4 — Knowledge Export and Sync

#### US-18b — Completion-triggered run export
**Derived gap from US-18**

As a PM, I want completed runs exported automatically to the decision-system repository so external compatibility does not depend on a manual follow-up step.

**Acceptance criteria**

- successful runs with Stage 7 output invoke `run_exporter` automatically
- export path collisions are handled predictably
- export failure policy is explicit and tested

**Dependencies**

- completion orchestration from US-25

### E5 — Operational Automation and Quality Safety

#### US-23b — Eval verification in standard workflow
**Derived gap from US-23**

As a maintainer, I want the eval harness used as a standard completion gate so routing quality remains measurable, not merely documented.

**Acceptance criteria**

- document a default eval command and expected environment
- ensure scenarios remain aligned with current routing rules
- add a lightweight contributor path to run eval before closing major workflow changes

---

## Later

### E1 — Signal Intake and Triage

#### US-06 — Automated signal collection
**Status:** Planned

As a PM, I want new signals collected automatically from configured sources so I do not rely only on manual entry.

**Acceptance criteria**

- implement RSS and file-watch collectors
- load per-product sources from the decision-system repository
- deduplicate inputs predictably before inserting signals
- assign `source_type` accurately for collected signals

**Dependencies**

- scheduler runtime wiring
- repository path contracts for source definitions

### E5 — Operational Automation and Quality Safety

#### US-24 — Scheduler wiring
**Status:** Planned

As a PM, I want scheduled collection jobs wired into the runtime so the platform operates without manual polling.

**Acceptance criteria**

- runtime startup creates scheduler jobs for configured collection flows
- local development and production startup semantics are documented
- scheduler lifecycle is compatible with FastAPI app lifespan

**Dependencies**

- automated signal collection implementation

---

## Sequencing Notes

- Do not build scheduler wiring before collection services exist.
- Do not automate export and wiki sync in multiple places; centralize completion side effects first.
- Do not broaden product promises in the main PRD before verification catches up with the implemented surface.

---

## Exit Criteria

The reverse roadmap can be considered largely closed when:

- completed runs automatically export and sync as intended
- automated signal collection is operating from configured sources
- integration coverage exists for the main decision and review flows
- Stage 6 and Stage 7 behavior is covered by unit tests
- completion semantics are explicit, consistent, and observable
