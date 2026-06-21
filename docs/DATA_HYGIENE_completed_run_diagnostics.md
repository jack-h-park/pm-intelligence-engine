# Data Hygiene — diagnosing "synthetic-looking" completed runs

**Status:** reference / contract note
**Origin:** 2026-06-21 investigation into 3 `workflow_runs` rows that *looked*
synthetic (never-executed placeholders) but are in fact real, fully-executed
pipeline runs. Documented here so the ops/freshness consumers stop
misclassifying them (`ops-new-signal-misclassification`, root cause #2).

---

## TL;DR

A completed `workflow_runs` row that has

- `current_stage` NULL/empty, **and**
- `created_at == updated_at` (to the microsecond),

is **NOT** evidence of a synthetic / never-executed run. Both signals are
expected for legitimate historical runs:

| Apparent "synthetic" marker | Why it is actually normal |
|---|---|
| `current_stage IS NULL` | `finalize_run` clears it on every terminal transition — `update_run(run_id, status=..., current_stage=None, ...)` ([app/services/run_finalizer.py](../app/services/run_finalizer.py) `finalize_run`). **Every** completed/killed run has `current_stage = NULL`. It is the *terminal* marker, not a *synthetic* one. |
| `created_at == updated_at` | The `updated_at` column was added in commit `4ab5d11` ("expose run.updated_at for gate-watcher dedup") and **backfilled from `created_at`** for all pre-existing rows ([app/models/workflow.py](../app/models/workflow.py) `WorkflowRun.updated_at`). Rows created before that deploy (~2026-06-20) and not updated afterward keep `updated_at == created_at`. It marks a *pre-deploy* row, not an *instant* completion. |

### The correct test for "did this run actually execute?"

A real run produces **stage outputs**. A truly synthetic/placeholder
`completed` row would have **zero** rows in `stage_outputs`.

```sql
-- Truly synthetic completed runs: completed but executed no stage at all.
-- (On the live DB as of 2026-06-21 this returns ZERO rows.)
SELECT r.run_id, r.product_id, r.signal_id, r.mode, r.status
FROM workflow_runs r
WHERE r.status = 'completed'
  AND (SELECT COUNT(*) FROM stage_outputs s WHERE s.run_id = r.run_id) = 0;
```

Do **not** use `created_at == updated_at AND current_stage IS NULL` as a
"never executed" proxy — it produces false positives on every pre-2026-06-20
completed run.

---

## The 2026-06-21 finding in full

Three rows were flagged as synthetic:

| run_id | product_id | mode | stage_outputs | artifacts | completed_at − created_at |
|---|---|---|---|---|---|
| `9a240c37-…` | example-enterprise-ai-product | structure | 3 | 0 | +11 min |
| `25a05ecf-…` | example-governance-product | note | 3 | 3 | +11 min (attempt #4 of a retry lineage) |
| `9b49fc8c-…` | example-security-product | evaluate | 8 | 5 | +9 h (paused at Gate 1; has a `direction` approval event) |

All three:

- ran real pipelines (3–8 `stage_outputs`),
- produced artifacts (two of them),
- have `completed_at` strictly **after** `created_at` (so they did not complete
  "instantly"),
- went **through** `finalize_run` (only `update_run`'s status-transition branch
  stamps `completed_at` — [app/storage/sqlite_store.py](../app/storage/sqlite_store.py) `update_run`), i.e. they did *not* bypass the finalizer.

They appeared synthetic only because they predate the `updated_at` backfill and,
as terminal runs, carry `current_stage = NULL`.

### Why `created_at == updated_at` splits the table by date

A clean cutover confirms the backfill explanation:

- Every row with `created_at == updated_at` was created **2026-05-28 → 2026-06-11**
  (pre-deploy: `updated_at` backfilled to `created_at`, never bumped because
  `onupdate` did not yet exist).
- Every row with `updated_at` advancing past `created_at` was created
  **2026-06-20 onward** (post-deploy: `onupdate=_utc_now` now bumps it).

---

## Residual cosmetic inconsistency (optional repair)

For the historical rows, `updated_at` (= `created_at` from the backfill) is
**earlier** than `completed_at`, which violates the natural invariant
`updated_at >= completed_at` for a settled run. This is harmless to the engine
(nothing reads `updated_at` for pre-deploy rows) but can confuse a consumer that
treats `updated_at` as "last touched".

An **optional, review-gated** repair script is provided at
[scripts/sql/repair_updated_at_backfill.sql](../scripts/sql/repair_updated_at_backfill.sql).
It is **not** auto-applied and **not** run by any migration — it must be reviewed,
the DB backed up, and run manually. It only advances `updated_at` to
`completed_at` where the latter is strictly greater; it never deletes or
re-times a run.

---

## What was explicitly **not** done, and why

- **No row was deleted or edited.** The three rows are correct decision history;
  removing them would destroy real `structure` / `note` / `evaluate` outputs and
  break the retry lineage that `25a05ecf-…` anchors (attempt #4).
- **No producer-side code change.** There is no code path that creates a
  synthetic `completed` row; every completion goes through `finalize_run` after
  executing ≥1 stage. The `signals.status` reconciler
  ([app/services/signal_status.py](../app/services/signal_status.py)) already
  handles the genuinely-bypassed-finalizer case (the "Glasswing" incident) via
  `POST /signals/reconcile`.
