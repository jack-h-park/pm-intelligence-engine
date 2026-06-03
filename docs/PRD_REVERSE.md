# Reverse PRD
## jackhpark-pm-agentic-platform

**Version:** 0.1  
**Status:** Working Reverse Spec  
**Last updated:** 2026-05-24

---

## 1. Purpose

This document describes the product as it exists in the repository today, not only as originally planned. It captures:

- the current user problem the platform addresses
- the end-to-end workflow already implemented
- the epics and user stories that define the product surface
- the status of each major capability

This reverse PRD is intentionally paired with two companion documents:

- [Implementation Status](IMPLEMENTATION_STATUS.md) — evidence and code mapping
- [Reverse Roadmap](ROADMAP_REVERSE.md) — remaining gaps and sequencing

---

## 2. Product Summary

`jackhpark-pm-agentic-platform` is a personal PM intelligence platform that automates the signal-to-decision workflow across three layers:

```text
SENSE -> DECIDE -> LEARN
```

The product reads workflow context and prompt assets from `jackhpark-pm-decision-context`, processes incoming market or platform signals through a structured seven-stage workflow, and writes decision outputs back to external repositories for long-term use.

### Core problem

A PM's judgment is high leverage, but the surrounding work is not:

- signals are easy to miss or collect inconsistently
- the same workflow must be rewritten manually across multiple markdown files
- decision rationale is hard to query and compare over time
- completed decisions do not automatically flow back into the knowledge base

### Current product promise

The PM can submit a signal, let the platform run a structured analysis pipeline, intervene only at explicit human gates, and receive a persisted decision artifact without manually authoring stage files.

---

## 3. Users and Scenarios

**Primary user:** a single PM operating the workflow directly

### Primary scenarios

| Scenario | Outcome | Status |
|---|---|---|
| Manual signal capture | A new signal is stored and available for workflow execution | Implemented |
| Guided run depth selection | The platform suggests how deeply to process a signal after Stage 2 | Implemented |
| Persona-based judgment review | The PM reviews four independent evaluations before committing to routing | Implemented |
| Decision artifact generation | The platform produces a PoC plan, PRD, or executive summary based on run depth | Implemented |
| Historical run lookup | The PM queries prior runs by product, status, or routing | Implemented |
| Automated signal intake | Signals arrive through RSS polling or file watch without manual entry | Planned |
| Automatic knowledge sync | Completed executive summaries are written into the wiki automatically | Partially Implemented |

---

## 4. End-to-End Workflow

### Current flow

1. A signal is created through `POST /signals`.
2. A workflow run is started through `POST /runs/start`.
3. Stage 1 normalizes the signal.
4. Stage 2 extracts insight, assigns a relevance score, and recommends a run mode.
5. If no mode was provided up front, the run pauses at the direction gate.
6. Depending on the chosen mode, the workflow continues through some or all of Stages 3 through 7.
7. In `decide` mode, the run pauses after Stage 4 for PM approval.
8. If Stage 5 routes to `kill`, the run pauses again for routing review.
9. The selected artifact is stored in the database and can be exported to the decision-system repository.

### Supported run modes

| Mode | Stages | Purpose | Status |
|---|---|---|---|
| `file` | S1, S2 | Low-value signal filing and early exit | Implemented |
| `brief` | S1, S2, S7 | Insight capture without opportunity pursuit | Implemented |
| `opportunity` | S1, S2, S3 | Opportunity framing only | Implemented |
| `evaluate` | S1, S2, S3, S4 | Full persona evaluation without routing | Implemented |
| `decide` | S1-S7 with gates | Full decision workflow | Implemented |

### Human-controlled gates

| Gate | Decision | Status |
|---|---|---|
| Direction gate | Confirm or override the recommended run depth after Stage 2 | Implemented |
| Evaluation gate | Approve, revise, or reject after Stage 4 in `decide` mode | Implemented |
| Routing review gate | Confirm a kill or override to `poc` / `prd` after Stage 5 | Implemented |

---

## 5. Epic Model

The reverse PRD uses five persistent epic IDs across all companion documents.

### E1 — Signal Intake and Triage
**Status:** Implemented, Partially Verified

The product must capture incoming signals, normalize them, and determine the minimum analysis depth required.

See implementation evidence in [Implementation Status: E1](IMPLEMENTATION_STATUS.md#e1--signal-intake-and-triage).

### E2 — Guided Decision Workflow
**Status:** Implemented, Partially Verified

The product must run a structured, human-gated decision workflow that preserves judgment quality and traceability.

See implementation evidence in [Implementation Status: E2](IMPLEMENTATION_STATUS.md#e2--guided-decision-workflow).

### E3 — Decision Artifact Generation
**Status:** Implemented, Partially Verified

The product must convert workflow outcomes into explicit next-step artifacts: PoC plan, PRD, and executive summary.

See implementation evidence in [Implementation Status: E3](IMPLEMENTATION_STATUS.md#e3--decision-artifact-generation).

### E4 — Knowledge Export and Sync
**Status:** Partially Implemented

The product must export completed runs to external repositories so automated workflows remain compatible with the manual system and the PM wiki.

See implementation evidence in [Implementation Status: E4](IMPLEMENTATION_STATUS.md#e4--knowledge-export-and-sync).

### E5 — Operational Automation and Quality Safety
**Status:** Partially Implemented

The product must automate recurring collection work and provide enough regression coverage to operate safely over time.

See implementation evidence in [Implementation Status: E5](IMPLEMENTATION_STATUS.md#e5--operational-automation-and-quality-safety).

---

## 6. User Stories

Statuses:

- `Implemented`: repository behavior exists and is wired into the product surface
- `Partially Implemented`: some code exists but orchestration, coverage, or completion criteria are incomplete
- `Planned`: documented intent exists but no implementation was found
- `Verified`: tests in this repository currently cover the behavior

### E1 — Signal Intake and Triage

| ID | Story | Status |
|---|---|---|
| US-01 | As a PM, I want to create a signal manually so I can capture a relevant event without leaving my workflow. | Implemented, Verified |
| US-02 | As a PM, I want to list and inspect stored signals so I can decide what to analyze next. | Implemented |
| US-03 | As a PM, I want the system to normalize and categorize raw signal text so downstream stages begin from structured input. | Implemented, Verified |
| US-04 | As a PM, I want Stage 2 to tell me how relevant a signal is and which mode it recommends so I can control workflow depth with less friction. | Implemented, Verified |
| US-05 | As a PM, I want low-relevance signals to be auto-triaged into a file/archive path so obvious noise does not interrupt me. | Implemented |
| US-06 | As a PM, I want new signals to be collected automatically from configured sources so I do not rely only on manual entry. | Planned |

### E2 — Guided Decision Workflow

| ID | Story | Status |
|---|---|---|
| US-07 | As a PM, I want to start a run from a stored signal so the workflow becomes a persisted unit of work. | Implemented |
| US-08 | As a PM, I want the run to pause after Stage 2 when no mode is preselected so I can confirm or override the recommended depth. | Implemented |
| US-09 | As a PM, I want Stage 3 to frame a falsifiable opportunity so later evaluation is grounded in a concrete hypothesis. | Implemented, Verified |
| US-10 | As a PM, I want four independent personas to evaluate the same opportunity in parallel so I reduce single-viewpoint bias. | Implemented, Verified |
| US-11 | As a PM, I want to approve, revise, or reject the evaluation before routing happens so the system never bypasses judgment. | Implemented |
| US-12 | As a PM, I want a routed kill decision to pause for review so a questionable blocking-assumption classification can be overridden. | Implemented |

### E3 — Decision Artifact Generation

| ID | Story | Status |
|---|---|---|
| US-13 | As a PM, I want the system to compute a deterministic composite score and routing so downstream actions are explainable. | Implemented, Verified |
| US-14 | As a PM, I want a PoC plan when confidence is not high enough for a PRD so the next step is still concrete. | Implemented |
| US-15 | As a PM, I want a structured PRD when the opportunity is strong enough so engineering can act on it. | Implemented |
| US-16 | As a PM, I want an executive summary for the run depth actually executed so stakeholders can understand the result quickly. | Implemented |
| US-17 | As a PM, I want stage outputs and generated artifacts stored in structured form so the run remains queryable and exportable. | Implemented |

### E4 — Knowledge Export and Sync

| ID | Story | Status |
|---|---|---|
| US-18 | As a PM, I want completed runs exported to the decision-system folder structure so automated runs remain compatible with the manual archive. | Implemented |
| US-19 | As a PM, I want auto-triaged signals archived into the wiki so I can audit what was dropped. | Implemented |
| US-20 | As a PM, I want completed executive summaries synced into the wiki automatically so learning accumulates without manual copying. | Partially Implemented |

### E5 — Operational Automation and Quality Safety

| ID | Story | Status |
|---|---|---|
| US-21 | As a PM, I want unit tests around core stage behavior so refactors do not silently break the workflow. | Implemented, Verified |
| US-22 | As a PM, I want integration coverage for real workflow paths so state transitions and orchestration are validated end to end. | Partially Implemented |
| US-23 | As a PM, I want an eval harness against historical scenarios so routing quality stays measurable over time. | Implemented |
| US-24 | As a PM, I want scheduled collection jobs wired into the runtime so the platform operates without manual polling. | Planned |
| US-25 | As a PM, I want stable operational semantics for terminal statuses and export/sync side effects so completed runs are trustworthy. | Partially Implemented |

---

## 7. Product Acceptance View

At the current repository state, the product can already support the following baseline flow:

1. Store a signal.
2. Start a run.
3. Execute S1-S7 depending on selected mode.
4. Pause at human gates where judgment is required.
5. Persist stage outputs and artifacts.
6. Query the resulting run history.
7. Export a completed run to the decision-system repository format.

The largest remaining product gaps are:

- automated signal collection is not wired into runtime behavior
- wiki sync for completed Stage 7 outputs is not fully orchestrated from the main workflow
- end-to-end verification coverage is thinner than the implemented feature surface

Those gaps are prioritized in [Reverse Roadmap](ROADMAP_REVERSE.md).
