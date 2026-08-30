-- repair_composite_score_backfill.sql
-- =============================================================================
-- OPTIONAL, REVIEW-GATED DATA CORRECTION — NOT auto-applied, NOT run by any
-- migration. Run manually only after review and an explicit go-ahead.
-- =============================================================================
--
-- Context
-- -------
-- `run_stage("s5")` wrote only the routing onto the run row and never the
-- composite score beside it, and that call was the ONLY writer of
-- `workflow_runs.composite_score`. The column was therefore NULL on every run
-- that ever reached S5 — 2 of 2 on the live DB when this was found. Fixed
-- forward in #73, which does not touch rows already written.
--
-- The score is not lost. S5 wrote its full output to `stage_outputs`, so the
-- value sits in the same database one join away; only the copy onto the run row
-- was missing. This script moves it, and reads nowhere else — it recovers a
-- number this system computed and recorded, it does not derive, estimate or
-- invent one.
--
-- Why it matters beyond a NULL: `portfolio_synthesis._run_summary` reads the
-- score off the RUN ROW to build the synthesis prompt, and has been sending
-- "composite: —" for every product since the column existed.
--
-- What it does
-- ------------
-- For runs with `composite_score IS NULL`, copies the score from the newest s5
-- `stage_outputs` row that carries one. Idempotent: once applied the rows no
-- longer match `composite_score IS NULL`, so re-running changes nothing. It
-- never overwrites a score that is already set, and never writes NULL — a run
-- with no s5 output, or an s5 output with no score, is left exactly as it is.
--
-- How to run (on the iMac, against the live DB) — AFTER APPROVAL ONLY:
--   cd ~/workspace/code/core/pm-intelligence-engine
--   cp pm_platform.db "pm_platform.db.bak.$(date +%Y%m%d-%H%M%S)"   # backup first
--   sqlite3 pm_platform.db < scripts/sql/repair_composite_score_backfill.sql
--
-- Dry-run first (preview the affected rows and the value each would receive,
-- mutate nothing):
--   sqlite3 -header -column pm_platform.db \
--     "SELECT wr.run_id, wr.routing, wr.composite_score AS current,
--             json_extract(so.output_json, '\$.output.composite_score') AS from_s5
--        FROM workflow_runs wr
--        JOIN stage_outputs so ON so.run_id = wr.run_id AND so.stage = 's5'
--       WHERE wr.composite_score IS NULL
--       ORDER BY wr.created_at;"
-- =============================================================================

BEGIN;

UPDATE workflow_runs
   SET composite_score = (
         SELECT json_extract(so.output_json, '$.output.composite_score')
           FROM stage_outputs so
          WHERE so.run_id = workflow_runs.run_id
            AND so.stage = 's5'
            AND json_extract(so.output_json, '$.output.composite_score') IS NOT NULL
          ORDER BY so.created_at DESC
          LIMIT 1
       )
 WHERE composite_score IS NULL
   -- Repeated in full rather than a bare EXISTS on "some s5 row": the SET picks
   -- the newest s5 row that HAS a score, so the guard has to be the same row, or
   -- a run whose newest s5 output lost its score would be written back to NULL.
   AND (
         SELECT json_extract(so.output_json, '$.output.composite_score')
           FROM stage_outputs so
          WHERE so.run_id = workflow_runs.run_id
            AND so.stage = 's5'
            AND json_extract(so.output_json, '$.output.composite_score') IS NOT NULL
          ORDER BY so.created_at DESC
          LIMIT 1
       ) IS NOT NULL;

-- Verify afterwards (should return 0 — no run with an s5 score still NULL):
--   SELECT COUNT(*) FROM workflow_runs wr
--    WHERE wr.composite_score IS NULL
--      AND EXISTS (SELECT 1 FROM stage_outputs so
--                   WHERE so.run_id = wr.run_id AND so.stage = 's5'
--                     AND json_extract(so.output_json, '$.output.composite_score') IS NOT NULL);

COMMIT;
