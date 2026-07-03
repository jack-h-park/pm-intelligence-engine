# Step 6 — Coordinated (position, lifecycle) cutover runbook

**Status:** Not executed. This is the executable checklist for the final step of
the redesign (WORKFLOW_MODEL_REDESIGN.md). Steps 1–5 are merged; this covers the
physical schema flip + observatory + Hermes + data wipe, which must land **in one
window** because the observatory reads the engine SQLite directly and Hermes polls
the API.

Coupling here was measured line-level (2026-07-03) against the real repos:
- Observatory `~/workspace/code/core/jackhpark-pm-observatory`
- Hermes `~/workspace/ai-assets/jackhpark-hermes-control-plane`

---

## 0. Principle & why it's now small

Steps 1–5 already did the hard parts:
- **Flow** is one registry-driven planner (`app/runner.py`) — no per-gate branching.
- **API** already has the target `POST /runs/{id}/decision` (the 6 legacy routes still exist alongside it).
- **Vocabulary** is already derivable and exposed: `app/run_view.py` maps
  `status/mode/current_stage` → `lifecycle/position/target/outcome/reason`, and
  the run object already returns those fields.

So this step is mechanical: (a) make the derived fields the *stored* ones, (b)
point the two consumers at them, (c) delete the legacy names, (d) wipe + reseed.
The `run_view` mapping table IS the spec for both the engine flip and the
observatory port — copy it, don't reinvent it.

---

## 1. Pre-cutover (do BEFORE the window — safe, independent)

These can ship ahead of time; none of them break the running system:

1. **Consumers read the new fields.** Update observatory + Hermes to *read*
   `lifecycle/position/outcome/reason` from the API (already present via step 5)
   and to *write* via `POST /decision`. While the engine still also returns the
   legacy fields, this is a no-op switch that can be verified live before the flip.
2. **Confirm signal preservation** (see §5): 24/32 signals are in
   `WIKI_ROOT/raw/from-web/sensing/`; the landmark manual signal `9b49fc8c`
   (Android 16 MTE) is only in the prod DB — export it now.
3. **Freeze**: no new runs started during the window.

---

## 2. Engine changes (the physical flip)

Do the flip so the `run_view` mapping becomes the storage, not a projection.

| File | Change |
|------|--------|
| `app/models/workflow.py` | Replace `RunStatus`/`RunMode` enums with `Lifecycle` (running/paused/done) and `Outcome` (completed/stopped/failed). Columns: drop `status`, `mode`, `ended_by`; add `lifecycle`, `position`, `target`, `outcome`, `reason`. Drop the `_missing_` legacy-alias hooks. |
| `app/storage/sqlite_store.py` | New schema (fresh DB — no ALTER migration path needed post-wipe). `update_run`: stamp `outcome` + `completed_at` when `lifecycle→done`; **stop clearing `position` on finalize** (it now records where the run ended). Serializer returns the new columns. Delete the state-rename UPDATEs (`pending`, `awaiting_direction`). |
| `app/services/run_finalizer.py` | Set `lifecycle="done"`, `outcome`, `reason` instead of `status`. `_derive_ended_by` → fold into `outcome`+`reason`. `run_view.project()` logic moves here / into the store. |
| `app/services/signal_status.py` | Derive signal status from run `outcome` (`completed`/`stopped` = resolved, `failed` = retryable) instead of `status`. |
| `app/runner.py` | `update_run(current_stage=…)` → `update_run(position=…)`. |
| `app/api/runs.py` (`_execute_s1_s2`), `direction.py`, `approvals.py`, `routing_review.py`, `void.py`, `deepen.py` | Set `lifecycle="paused"` + `position` (which gate) instead of `status="waiting_*"`; set `lifecycle="running"` instead of `status="running"`. |
| `app/api/decision.py` | Dispatch on `lifecycle`+`position` instead of `status`. The action vocabulary and response are unchanged (that was the point of step 4). |
| `app/api/decision.py` + legacy routers | **Delete** the 6 legacy endpoints (`/direction`, `/approve`, `/revise`, `/reject`, `/routing-review`, `/void`, `/reopen`, `/deepen`) and their routers from `main.py`; `/decision` is the sole entry. |
| `app/run_view.py` | Delete — its mapping now lives in the store/model. |
| `app/modes.py` | Delete or reduce to the registry-derived depth list; drop `normalize_mode` legacy aliases. |
| Tests | Rewrite `status`/`mode`/`ended_by` assertions to `lifecycle`/`position`/`outcome`/`reason`. This is the bulk of the diff; the `run_view` and `run_finalizer` test matrices already encode the correct expected values. |

Legacy aliases retired here (all three): `file/brief/opportunity`,
`awaiting_direction`, signal `pending`.

---

## 3. Observatory changes (`jackhpark-pm-observatory`)

Bounded to the adapter/format layer. **Port the `run_view` mapping to TS** rather
than inventing one.

| File | Change |
|------|--------|
| `lib/adapters/engine-db.ts` | The raw SQL reads `status`, `current_stage` and filters on `waiting_direction/approval/routing_review` (≈ lines 117–124, 192, 238–258). Repoint to the new columns: gate queues → `WHERE lifecycle='paused'` (+ `position` for which gate); status counts (`SELECT status, COUNT`) → group by `lifecycle`/`outcome`; `current_stage`/`effective_stage` → `position`. |
| `lib/pipeline-stages.ts` | `GATE_BY_STATUS` (`waiting_direction→g1`, …) → derive the gate from `position` when `lifecycle='paused'` (`s2→g1, s4→g2, s5→g3`). `nodeForStage` already keys on the stage id + routing — keep as-is (it's already position-based). |
| `lib/format.ts` | `STATUS_LABELS` / `STATUS_TONE` currently key on `status`. Re-key on `lifecycle`+`outcome` (paused→gate label via `position`; done+completed→"Completed"; done+stopped→"Stopped"; done+failed→"Failed"). Drop the `awaiting_direction` legacy key. |
| `components/GateActions.tsx` | The gate action buttons call the per-gate endpoints → call `POST /runs/{id}/decision` with the matching `{action, …}` (see the transition table in API_CONTRACT). |
| `lib/integrity.ts` | Recheck any status-based integrity heuristics against the new fields. |

---

## 4. Hermes changes (`jackhpark-hermes-control-plane`) — mostly prose skills

Iris is an LLM agent following SKILL/SOUL instructions, so most of this is editing
markdown, not code.

| File | Change |
|------|--------|
| `distributions/hermes-ops/SOUL.md` | Gate table (lines ≈ 86–88: `waiting_direction`/`waiting_approval`/`waiting_routing_review`) → `lifecycle='paused'` + `position`. Gate action calls (≈ 226–345: `POST /runs/{id}/direction|approve|revise|reject|routing-review`) → `POST /runs/{id}/decision {"action": …}`. The note that "the engine clears `current_stage` to null on finalize" (≈ 238–246) is now **false** — `position` persists after finalize; update the live-vs-terminal stage reporting accordingly. |
| `distributions/hermes-eval/SOUL.md` | Digest gate counts (line ≈ 33: `gates: waiting_direction N, …`) → count by `lifecycle='paused'` grouped by `position`. |
| `distributions/hermes-ops/skills/gate-watcher/SKILL.md` | Poll one queue `lifecycle='paused'` instead of three `waiting_*` statuses; render generically by `position`. |
| `hermes_eval/*.py` (`detect_alerts`, `render_digest`, `build_snapshot`) | Repoint any `status`/`routing` reads to `lifecycle`/`outcome`. |

Notification ownership (US-48/50) is unchanged: Iris still owns delivery; only the
field names it reads change.

---

## 5. Data — wipe & reseed

The 42 prod runs are unvalidated dry-runs (memory `golden-set-not-ground-truth`);
migrate nothing.

1. **Backup**: copy `pm_platform.db` to `pm_platform.db.bak-cutover-<date>` on the iMac.
2. **Export the landmark** before wipe: pull signal `9b49fc8c`'s row from the prod
   DB (title + raw_content) — it is manual, not in the wiki.
3. **Wipe**: stop the engine, delete `pm_platform.db`; the engine recreates an
   empty schema on next start (SQLAlchemy `create_all` + `_migrate_schema`).
4. **Reseed signals**: re-submit via `POST /signals` — the 24 wiki-backed sensing
   files through the normal Gate 0 path, plus the landmark from step 2. The other
   7 orphans are noise; drop them.

---

## 6. Deploy sequence (one window)

Hard cutover — all three repos change together; no dual-read window (data wiped).

1. Freeze intake (no new runs).
2. **Engine**: deploy new schema/code to the iMac, wipe DB (§5), restart via launchd.
3. **Observatory**: deploy the adapter/format changes; verify it renders the empty
   (then reseeded) DB without errors.
4. **Hermes**: update the SOUL/SKILL markdown + `hermes_eval` reads; restart the
   gate-watcher.
5. **Reseed** signals (§5.4).
6. **Smoke test** (§7).

**Rollback**: `git revert` the three repos to the pre-cutover commits and restore
`pm_platform.db.bak-cutover-<date>`. Because it's a hard break, roll back all three
together — a half-rolled-back state (new engine + old observatory) is the one to
avoid.

---

## 7. Smoke test (post-cutover acceptance)

Drive one signal the whole way through `/decision` only:

1. `POST /signals` → run auto-created / started.
2. Gate 1: `POST /runs/{id}/decision {"action":"advance_to","target":"decide"}`.
3. Gate 2: `… {"action":"advance"}` → routes to S5, pauses at Gate 3.
4. Gate 3: `… {"action":"advance"}` (or `advance_to`/`stop`) → S6→S7→done.
5. Verify: run `GET /runs/{id}` shows `lifecycle=done, outcome=completed,
   position=s7`; observatory renders the run + gates correctly; Iris delivered
   each gate prompt on Discord.
6. Verify a `note` run stops at `position=s2` with no S7 (clean-model behavior
   from step 2).

Also confirm the legacy endpoints now return 404 (they were deleted) and nothing
in observatory/Hermes still calls them.

---

## 8. Sizing (honest)

- **Engine**: ~1 day. The logic is done; this is column renames + dispatch on the
  new fields + a large but mechanical test-assertion rewrite (the expected values
  already exist in the `run_view`/`run_finalizer` test matrices).
- **Observatory**: ~half a day. 4 files, mapping ported from `run_view`.
- **Hermes**: ~2–3 hours. Mostly markdown; the `hermes_eval` reads are small.
- **Window itself**: ~1 hour incl. wipe, reseed, smoke test.

The risk is coordination, not volume — hence this runbook. Nothing here needs new
design decisions; it executes the already-approved model.
