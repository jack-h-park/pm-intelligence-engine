-- repair_updated_at_backfill.sql
-- =============================================================================
-- OPTIONAL, REVIEW-GATED DATA CORRECTION — NOT auto-applied, NOT run by any
-- migration. Run manually only after review and an explicit go-ahead.
-- =============================================================================
--
-- Context
-- -------
-- The `workflow_runs.updated_at` column was added in commit 4ab5d11 and
-- backfilled from `created_at` for all pre-existing rows. For rows that had
-- already settled before that deploy (~2026-06-20), this leaves
--   updated_at (= created_at) < completed_at
-- which violates the natural invariant `updated_at >= completed_at` for a
-- settled run. This is harmless to pm-engine itself (it does not read
-- updated_at for pre-deploy rows), but it can confuse an external consumer that
-- reads updated_at as "last touched".
--
-- See docs/DATA_HYGIENE_completed_run_diagnostics.md for the full finding. Note
-- in particular: these rows are REAL, fully-executed runs — this script only
-- repairs a timestamp, it does NOT delete or re-time any run.
--
-- What it does
-- ------------
-- Advances `updated_at` to `completed_at` ONLY where completed_at is strictly
-- greater. Idempotent: re-running changes nothing once applied. It never moves a
-- timestamp backward and never touches rows whose updated_at is already correct
-- (post-deploy rows, failed rows with no completed_at).
--
-- How to run (on the iMac, against the live DB) — AFTER APPROVAL ONLY:
--   cd ~/workspace/code/core/pm-intelligence-engine
--   cp pm_platform.db "pm_platform.db.bak.$(date +%Y%m%d-%H%M%S)"   # backup first
--   sqlite3 pm_platform.db < scripts/sql/repair_updated_at_backfill.sql
--
-- Dry-run first (preview the affected rows, mutate nothing):
--   sqlite3 -header -column pm_platform.db \
--     "SELECT run_id, status, created_at, updated_at, completed_at
--        FROM workflow_runs
--       WHERE completed_at IS NOT NULL AND updated_at < completed_at
--       ORDER BY created_at;"
-- =============================================================================

BEGIN;

UPDATE workflow_runs
   SET updated_at = completed_at
 WHERE completed_at IS NOT NULL
   AND updated_at < completed_at;

-- Verify the invariant holds afterward (should return 0):
--   SELECT COUNT(*) FROM workflow_runs
--    WHERE completed_at IS NOT NULL AND updated_at < completed_at;

COMMIT;
