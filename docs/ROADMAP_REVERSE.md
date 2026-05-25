# Reverse Roadmap
## Remaining Gap Closure Backlog

**Version:** 0.2  
**Last updated:** 2026-05-24

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
  `raw/from-decision-system/kills/auto-triaged/`
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
**Status:** In progress

As a maintainer, I want UTC handling to be explicit and warning-free so
terminal metadata remains stable across Python/SQLAlchemy upgrades.

**Acceptance criteria**

- remove deprecated `datetime.utcnow()` usage
- keep `completed_at` semantics unchanged for completed/killed/failed
- ensure tests pass without timestamp behavior regressions

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
**Status:** In progress

As a Hermes implementer, I want to validate that the new artifact endpoint is
sufficient for wiki sync and notifications before broadening the API further.

**Acceptance criteria**

- validate `GET /runs/{id}/artifacts` against Hermes sync needs
- keep `GET /runs/{id}?include_outputs=true` for stage-level inspection
- only add further artifact-specific endpoints if Hermes proves this surface insufficient

---

## Later

## Exit Criteria

The remaining roadmap can be considered closed when:

- Hermes owns all wiki writes, including auto-triage archive
- eval outcomes are calibrated for the chosen model baseline
- artifact access is stable enough that Hermes does not need DB/file knowledge
- export behavior is explicit and observable under retries and collisions
