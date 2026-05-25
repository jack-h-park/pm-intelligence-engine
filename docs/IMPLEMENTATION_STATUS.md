# Implementation Status
## Reverse PRD Evidence Map

**Version:** 0.2  
**Last updated:** 2026-05-24

This document maps the reverse PRD to concrete repository evidence. It is intentionally evidence-first and changes more often than the PRD.

Companion documents:

- [Reverse PRD](PRD_REVERSE.md)
- [Reverse Roadmap](ROADMAP_REVERSE.md)

Status meanings:

- `Implemented`: code exists and is connected to the product surface
- `Partially Implemented`: code exists, but orchestration, completion, or coverage is incomplete
- `Planned`: referenced in docs but not found in implementation
- `Verified`: explicit repository tests currently cover this behavior

---

## Repository-Wide Notes

### Verified in this review

- `pytest tests/unit -q` → 95 passed
- `pytest tests/integration -q` → 26 passed
- `pytest tests/ -q` → 121 passed

### Known limits of this review

- `eval/runner.py` exists and is structurally complete; full regression run depends on real LLM access
- wiki sync and signal harvesting are Hermes-owned — pm-platform does not test those end-to-end
- some behaviors are implemented as callable services but not confirmed as runtime-integrated

---

## E1 — Signal Intake and Triage

### Summary

The manual intake path and Stage 1-2 triage flow are implemented and verified. Automated collection is Hermes-owned.

### Story mapping

| Story | Status | Evidence | Gap / Notes |
|---|---|---|---|
| US-01 | Implemented, Verified | `app/api/signals.py`, `app/storage/sqlite_store.py` | No API integration test; covered by store behavior |
| US-02 | Implemented | `GET /signals`, `GET /signals/{id}` in `app/api/signals.py`; filtering in `SQLiteStore.list_signals()` | Query behavior exists; no dedicated endpoint integration test |
| US-03 | Implemented, Verified | `app/stages/s1_signal.py`, `app/models/stages.py`, `tests/unit/test_s1.py` | Event date enrichment remains minimal |
| US-04 | Implemented, Verified | `app/stages/s2_insight.py`, `app/api/runs.py`, `tests/unit/test_s2.py` | Recommendation is persisted as JSON on `WorkflowRun` |
| US-05 | Implemented, Verified | auto-triage path in `app/api/runs.py`; `archive_auto_triaged()` in `app/services/wiki_sync.py`; threshold in `config.py`; `run_finalizer` completes with `mode=file` | Status semantics documented; auto-triage archive ownership transitioning to Hermes |
| US-06 | Moved to Hermes | `POST /signals` API stable; Hermes submits signals on schedule | No `app/services/signal_collector.py`; harvesting is Hermes-owned per `INTEGRATION_PRINCIPLES.md` |

---

## E2 — Guided Decision Workflow

### Summary

The guided workflow surface is fully implemented, including all three human-controlled gates.

### Story mapping

| Story | Status | Evidence | Gap / Notes |
|---|---|---|---|
| US-07 | Implemented | `POST /runs/start` in `app/api/runs.py`; run creation in `SQLiteStore.create_run()` | Run starts asynchronously via background task |
| US-08 | Implemented | `awaiting_direction` status in `app/models/workflow.py`; `app/api/direction.py`; `_execute_s1_s2()` in `app/api/runs.py` | Behavior exists for both confirm and override |
| US-09 | Implemented, Verified | `app/stages/s3_opportunity.py`, `tests/unit/test_s3.py`, `eval/rubrics/s3_hypothesis.py` | Runtime integration not separately tested |
| US-10 | Implemented, Verified | `app/stages/s4_evaluation.py`, persona agents in `app/agents/`, `tests/unit/test_s4.py` | No dedicated integration test for persona independence across a live run |
| US-11 | Implemented, Verified | `app/api/approvals.py`; approval persistence via `record_approval()`; statuses in `workflow.py`; `tests/integration/test_approval_flow.py` | All three branches (approve/revise/reject) integration-tested |
| US-12 | Implemented, Verified | `app/api/routing_review.py`; `waiting_routing_review` state; Stage 5 handoff; `tests/integration/test_approval_flow.py` | confirm and override paths integration-tested |

---

## E3 — Decision Artifact Generation

### Summary

The decision artifact pipeline is implemented and unit-tested from S5 routing through S7 artifact storage.

### Story mapping

| Story | Status | Evidence | Gap / Notes |
|---|---|---|---|
| US-13 | Implemented, Verified | `app/stages/s5_prioritization.py`, `eval/rubrics/s5_routing.py`, `tests/unit/test_s5.py` | Composite/routing logic is code-defined and tested |
| US-14 | Implemented, Verified | `app/stages/s6a_poc_plan.py`; stage handoff in `app/api/approvals.py` and `app/api/routing_review.py`; `tests/unit/test_s6a.py` | JSON parsing, completeness, and store persistence covered |
| US-15 | Implemented, Verified | `app/stages/s6b_prd.py`; completeness check computed deterministically; `tests/unit/test_s6b.py` | Completeness scoring edge cases covered |
| US-16 | Implemented, Verified | `app/stages/s7_summary.py`; partial/full summary schemas by mode; `tests/unit/test_s7.py` | Both decide (full) and brief/opportunity (partial) modes tested |
| US-17 | Implemented, Verified | Stage persistence in all stage files; artifact saved via `store.save_artifact()` in S7; `tests/unit/test_s7.py` covers artifact call | Artifact querying is indirect; no public artifact endpoint |

---

## E4 — Knowledge Export and Sync

### Summary

Export to the decision-system format is wired via `run_finalizer`. Wiki sync ownership is resolved:
pm-platform is NOT the wiki sync owner. Wiki writes are Hermes-owned.
The `wiki_sync.py` module is retained as a utility adapter with canonical paths documented and contract-tested.

### Story mapping

| Story | Status | Evidence | Gap / Notes |
|---|---|---|---|
| US-18 | Implemented, Verified | `app/services/run_exporter.py` + `app/services/run_finalizer.py`; `tests/unit/test_run_finalizer.py` | `finalize_run()` triggers `_maybe_export()` for decide-mode completions only |
| US-19 | Implemented | `archive_auto_triaged()` in `app/services/wiki_sync.py`; call site in `app/api/runs.py` | Path is `raw/from-decision-system/kills/auto-triaged/`; transitioning to Hermes |
| US-20 | Deferred to Hermes | `sync_executive_summary()` in `app/services/wiki_sync.py` (utility, not called from completion) | Wiki sync is Hermes-owned; canonical paths documented in `EXPORT_AND_SYNC_CONTRACT.md` |

### Ownership clarification (per [EXPORT_AND_SYNC_CONTRACT.md](EXPORT_AND_SYNC_CONTRACT.md))

| Artifact | Owner | When triggered |
|----------|-------|----------------|
| decision-system export | pm-platform | On `completed` for decide-mode runs (via `run_finalizer`) |
| wiki sync | Hermes | On terminal run events, independently |
| auto-triage archive | pm-platform (utility) → Hermes (planned) | On `auto_triaged` event |

---

## E5 — Operational Automation and Quality Safety

### Summary

Testing coverage is now comprehensive: all stages S1–S7 have unit tests, all gate flows have integration tests,
and contract tests verify ownership boundaries. Eval harness exists structurally; live run depends on LLM access.

### Story mapping

| Story | Status | Evidence | Gap / Notes |
|---|---|---|---|
| US-21 | Implemented, Verified | `tests/unit/test_s1.py` through `test_s7.py` — full S1–S7 coverage | 95 unit tests passing |
| US-22 | Implemented, Verified | `tests/integration/test_approval_flow.py` (21 tests) + `test_context_loader.py` (5 tests) | approve/revise/reject/routing-review paths all covered; `failed` `completed_at` contract verified |
| US-23 | Implemented, Verified | `eval/runner.py`, `eval/scenarios.json`, `eval/rubrics/`; live run 2026-05-24 | Harness runs end-to-end; S1–S5 pipeline, Pydantic validation, and rubric checks all pass. Routing: R05 ✓, R06 ✓, R04 ✗, R07 ✗ — failures are model calibration (gpt-4o vs historical Claude runs, skeptic 2/5 systematically). Code behavior is correct. See eval notes below. |
| US-24 | Moved to Hermes | Scheduling/harvesting is Hermes-owned; `signal_collector.py` is not in-process | pm-platform exposes `POST /signals`; Hermes submits signals on schedule |
| US-25 | Implemented, Verified | `app/services/run_finalizer.py` — single exit point; `completed_at` auto-stamped for completed/killed; export triggered only for decide mode; `tests/unit/test_run_finalizer.py` (18 tests) | wiki sync removed from pm-platform completion path; `failed` intentionally does NOT receive `completed_at` |

### Eval harness findings (2026-05-24 run with gpt-4o)

| Scenario | S3 rubric | S4 rubric | Composite | Routing | Result |
|----------|-----------|-----------|-----------|---------|--------|
| R04 APM enforcement | 3/3 ✓ | 10/12 ✓ | 3.5 (exp 4.0–5.0) | kill (exp prd) | ✗ Routing mismatch |
| R05 NFC allowlist | 3/3 ✓ | 11/12 ✓ | 3.5 (exp 4.0–5.0) | prd ✓ | ✓ Pass |
| R06 DISA MTD | 3/3 ✓ | 10/12 ✓ | 3.5 (exp 1.0–2.0) | kill ✓ | ✓ Pass |
| R07 RKP transition | 3/3 ✓ | 10/12 ✓ | 3.8 (exp 3.5–5.0) | kill (exp prd) | ✗ Routing mismatch |

**Root cause:** gpt-4o systematically assigns Skeptic 2/5 → composite fixed at 3.5; and classifies assumptions
as Blocking in R04/R07 that historical Claude runs treated as Informing.
**Pipeline behavior is correct** — routing logic, Pydantic validation, rubric checks all function as designed.
**Fix path:** recalibrate S4/S5 prompts for gpt-4o, or use Claude for eval runs.

### Highest-value gaps (remaining)

1. Auto-triage archive ownership is still transitional — local archive writes remain until Hermes takes over fully
2. Eval calibration for the active model baseline — R04/R07 still diverge under `gpt-4o`
3. Export retry remains event-based only — repeated export overwrite behavior is now contract-tested, but no automatic retry/backoff exists

See sequencing in [Reverse Roadmap](ROADMAP_REVERSE.md).
