# Export and Sync Contract
## jackhpark-pm-agentic-platform — File Write Ownership

**Version:** 1.0  
**Last updated:** 2026-05-24

This document defines who writes what, when, and where. It is the canonical reference
for understanding side-effect ownership at run completion.

Companion documents:
- [API Contract](API_CONTRACT.md) — HTTP API surface (Hermes reads this)
- [Integration Principles](INTEGRATION_PRINCIPLES.md) — non-negotiable boundary rules

---

## Ownership Summary

| Artifact | Owner | Trigger | Destination |
|----------|-------|---------|-------------|
| decision-system run export | **pm-platform** | `completed` (decide mode only) | `DECISION_SYSTEM_ROOT/products/<name>/runs/<date>-<slug>/` |
| wiki executive summary sync | **Hermes** | `completed` or `killed` event (Hermes polls) | `WIKI_ROOT/raw/from-decision-system/{prds\|poc-upgrades\|kills}/` |
| auto-triage archive | pm-platform (current utility) → **Hermes** (planned) | `auto_triaged` event | `WIKI_ROOT/raw/from-decision-system/kills/auto-triaged/` |

---

## Mode-by-Mode Export/Sync Table

| Mode | decision-system export | wiki sync | auto-triage archive | Notes |
|------|------------------------|-----------|---------------------|-------|
| `decide` (completed) | ✅ pm-platform | ✅ Hermes | — | Full artifact set: S6 + S7 output |
| `decide` (killed — reject) | — | ✅ Hermes | — | Hermes reads artifacts, syncs kill record |
| `decide` (killed — routing kill confirmed) | — | ✅ Hermes | — | Same as reject |
| `decide` (routing override → completed) | ✅ pm-platform | ✅ Hermes | — | override changes routing, export still triggered |
| `poc` (completed) | ✅ pm-platform | ✅ Hermes | — | S6A + S7 artifacts |
| `prd` (completed) | ✅ pm-platform | ✅ Hermes | — | S6B + S7 artifacts |
| `kill` (via Gate 3 confirm) | — | ✅ Hermes | — | kill mode has no S6/S7 artifacts to export |
| `file` (auto-triage) | — | — | ✅ (see below) | No LLM stages ran beyond S2 |
| `brief` (completed) | — | — | — | Internal use; no external artifact sync |
| `opportunity` (completed) | — | — | — | S3 output only; not synced externally |
| `evaluate` (completed) | — | — | — | S4 output only; not synced externally |

> **Exportable modes:** `decide` only (and its routing variants `prd`, `poc`, `kill`).  
> Non-decide modes (`file`, `brief`, `opportunity`, `evaluate`) never trigger decision-system export.

---

## Decision-System Export (pm-platform owned)

### What triggers it
`run_finalizer.finalize_run()` is called with `status="completed"`. If the run's `mode`
is in `{"decide"}`, `_maybe_export()` is invoked.

### Where it writes
```
DECISION_SYSTEM_ROOT/products/<product_id>/runs/<YYYY-MM-DD>-<slug>/
```

### What it writes
- `run.json` — full run metadata and stage output references
- `s7_executive_summary.md` — Stage 7 Markdown artifact (if available)
- `s7_executive_summary.json` — Stage 7 JSON artifact (if available)

### Failure behavior
Export failures are **non-fatal**: if `DECISION_SYSTEM_ROOT` is not writable (e.g. network
drive unavailable), the event `run_exporter.export_skipped` is emitted and the run is still
marked `completed`. pm-platform does not retry exports.

### Idempotency
Re-exporting an already-exported run overwrites the directory. The export function is
idempotent by design — safe to call twice on the same run.

---

## Wiki Sync (Hermes owned)

### What triggers it
Hermes polls `GET /runs?status=completed` and `GET /runs?status=killed` periodically.
When new terminal runs appear, Hermes reads artifacts via `GET /runs/{id}?include_outputs=true`
and writes to `WIKI_ROOT`.

### Canonical target paths

| Routing | Wiki target path |
|---------|-----------------|
| `prd` | `WIKI_ROOT/raw/from-decision-system/prds/<filename>.md` |
| `poc` | `WIKI_ROOT/raw/from-decision-system/poc-upgrades/<filename>.md` |
| `kill` | `WIKI_ROOT/raw/from-decision-system/kills/<filename>.md` |

### Filename convention
`<YYYY-MM-DD>-<product_id>-<slug>.md`  
Example: `2026-05-24-samsung-knox-lockdown-mode-android-16-nfc-allowlist.md`

### Failure behavior
Hermes-owned; pm-platform has no visibility into wiki write failures.

### Idempotency
Hermes must ensure idempotency. The recommended pattern is to check for an existing file
before writing or to use a last-written-wins overwrite strategy.

---

## Auto-Triage Archive

### What triggers it
A run is auto-triaged when `relevance_score < AUTO_TRIAGE_THRESHOLD` at Stage 2. The run
completes immediately with `mode=file` and `status=completed`. The `event_action` emitted
is `auto_triaged`.

### Current behavior (v1 — pm-platform utility)
`archive_auto_triaged()` in `app/services/wiki_sync.py` is called from the auto-triage
path in `app/api/runs.py`. This is a non-fatal write to:

```
WIKI_ROOT/raw/from-decision-system/kills/auto-triaged/<YYYY-MM-DD>-<slug>.md
```

### Planned behavior (Hermes-owned)
In the target architecture, Hermes detects `auto_triaged` events by polling for completed
runs with `mode=file`, then writes the archive record. The current pm-platform call to
`archive_auto_triaged()` will be removed when Hermes takes over this path.

**Transition plan:** pm-platform utility call remains until Hermes signals it has taken over;
then the call site in `runs.py` is removed and `archive_auto_triaged()` is deprecated.

---

## pm-platform wiki_sync.py Status

`app/services/wiki_sync.py` is retained as a **utility adapter** only:
- It documents the canonical wiki paths
- It exposes `sync_executive_summary()` and `archive_auto_triaged()` as callable helpers
- It is **NOT called from any pm-platform completion path** (except the auto-triage legacy call)
- Hermes may call the equivalent logic directly in its own codebase

---

## What pm-platform Will Never Do

| Action | Reason |
|--------|--------|
| Write to `WIKI_ROOT/raw/from-decision-system/prds/` | Wiki sync is Hermes-owned |
| Write to `WIKI_ROOT/raw/from-decision-system/poc-upgrades/` | Wiki sync is Hermes-owned |
| Write to `WIKI_ROOT/raw/from-decision-system/kills/` (non-auto-triage) | Wiki sync is Hermes-owned |
| Call `sync_executive_summary()` from any completion path | Ownership conflict |
| Retry failed wiki writes | Not pm-platform's concern |
| Block run completion on wiki write success | Completion is independent of wiki state |
