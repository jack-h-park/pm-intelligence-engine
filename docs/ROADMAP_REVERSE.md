# Reverse Roadmap
## Remaining Gap Closure Backlog

**Version:** 0.3  
**Last updated:** 2026-05-25

This document contains only work that remains after the current verified baseline.
Implemented workflow, contracts, and tests are tracked in:

- [Reverse PRD](PRD_REVERSE.md)
- [Implementation Status](IMPLEMENTATION_STATUS.md)

---

## Now

### E4 — Hermes Handoff Completion

#### US-20b — Auto-triage archive ownership cutover
**Status:** In transition

As a maintainer, I want Hermes to own the auto-triage wiki archive fully so
pm-platform has no remaining wiki write responsibility.

**Acceptance criteria**

- Hermes archives `status=completed` + `mode=file` runs into
  `raw/from-pm-decision-context/kills/auto-triaged/`
- `AUTO_TRIAGE_LOCAL_ARCHIVE_ENABLED=false` is supported and documented
- pm-platform completion remains successful with local archive disabled
- legacy local archive call path can be removed after Hermes validation

### E5 — Quality Calibration

#### US-23b — Eval calibration for active model baseline
**Status:** Open

As a maintainer, I want eval outcomes calibrated for the active model baseline
so routing regressions are distinguishable from prompt/model drift.

**Acceptance criteria**

- choose and document the official eval model baseline
- reduce R04/R07 routing mismatches or explicitly bless them as model-specific variance
- document whether production/eval should prefer Claude or OpenAI
- keep `eval/scenarios.json` aligned with the chosen baseline

---

## Next

### E5 — Runtime Hygiene

#### US-25b — UTC and timestamp consistency cleanup
**Status:** ✅ Completed

`datetime.utcnow()` fully replaced with `datetime.now(UTC)` / `datetime.now(timezone.utc)`
across all modules. `completed_at` semantics unchanged; 128 tests pass without regressions.

### E4 — Export Observability

#### US-18c — Export collision and retry policy hardening
**Status:** Open

As a maintainer, I want export retries and path collisions to be predictable so
decision-system compatibility remains trustworthy.

**Acceptance criteria**

- keep overwrite behavior for repeated exports of the same run contract-tested
- make skip/failure behavior visible through emitted events
- decide whether retry/backoff belongs in pm-platform or Hermes monitoring

### E5 — API Consumer Ergonomics

#### US-17c — Artifact endpoint adoption validation
**Status:** ✅ Completed

`GET /runs/{id}/artifacts` implemented in `app/api/artifacts.py`, registered in
`main.py`, backed by `list_artifacts()` in store protocol + SQLiteStore, and
integration-tested in `tests/integration/test_artifacts_api.py`.
`GET /runs/{id}?include_outputs=true` retained for stage-level inspection.
Further artifact endpoints deferred until Hermes integration confirms need.

---

## Later

## Exit Criteria

The remaining roadmap can be considered closed when:

- Hermes owns all wiki writes, including auto-triage archive
- eval outcomes are calibrated for the chosen model baseline
- artifact access is stable enough that Hermes does not need DB/file knowledge
- export behavior is explicit and observable under retries and collisions
