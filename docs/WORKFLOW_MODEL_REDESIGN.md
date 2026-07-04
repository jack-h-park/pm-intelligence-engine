# Workflow Model Redesign — (position, lifecycle)

**Status:** Design target (approved 2026-07-03). Not yet implemented.
**Supersedes the shape of:** the depth ladder (`app/modes.py`), the run status
enum, `ended_by`, the three gate endpoints, and the four "terminate a run" paths.
**Related:** ROADMAP_REVERSE E5 (US-51/52/53/54), DESIGN_DECISIONS §4.

This document is the **fundamental** redesign the incremental fixes (deepen,
`ended_by`, blocking→poc) kept pointing at. Those fixes were additive patches on a
representation that is more complex than the behavior it encodes. This defines the
target so future changes converge instead of accreting more vocabulary.

---

## 1. The problem: five parallel vocabularies encode two facts

The engine has **five separate enums** describing a run, totalling ~40 values,
plus **six gate/terminal endpoints**:

| Enum | Values | File |
|------|--------|------|
| depth / `RunMode` | archive, note, structure, evaluate, decide | `app/modes.py`, `models/workflow.py` |
| `RunStatus` | pending, running, waiting_direction, waiting_approval, waiting_routing_review, completed, killed, failed | `models/workflow.py` |
| `ended_by` | auto_triaged, archived, noted, structured, evaluated, decided, rejected, kill_confirmed, kill_overridden, voided, failed | `services/run_finalizer.py` |
| `ApprovalAction` | direction, approve, revise, reject, confirm, override, void, reopen, deepen, auto_triaged | `models/workflow.py` |
| `ArtifactType` | insight_memo, opportunity_memo, evaluation_brief, decision_memo, poc_plan, prd, executive_summary, checkpoint | `models/workflow.py` |

Almost all of it is a projection of **two underlying facts**:

1. **Position** — how far the run got on the linear pipeline S1 → S7.
2. **Disposition** — how it left that position: advanced by a human, advanced
   automatically, stopped by a human, stopped by the system, or failed.

### Proof by isomorphism

**A. depth ≡ completed-`ended_by` ≡ head artifact.** All three encode "which
stage did this run stop at":

| stopped at | depth | ended_by | head artifact |
|---|---|---|---|
| S1 | archive | archived | (none) |
| S2 | note | noted | insight_memo |
| S3 | structure | structured | opportunity_memo |
| S4 | evaluate | evaluated | evaluation_brief |
| S7 | decide | decided | decision_memo / poc_plan / prd / executive_summary |

`run_finalizer._ENDED_BY_FROM_MODE` literally maps `mode → ended_by`. Three
spellings of one fact.

**B. The three gates are one pattern.** Gate 1/2/3 each: pause the run, show the
human the latest stage output, take a decision that advances or terminates. This
one pattern is implemented as 3 status values (`waiting_*`) + 3 endpoints
(`/direction`, `/approve|revise|reject`, `/routing-review`) + 3 notifier methods
(`send_gate1/2/3`) + 2 review builders (`_build_gate1_review`, `_build_gate3_review`).

**C. "Terminate a run" has four spellings.** archive (Gate 1) → completed; reject
(Gate 2) → killed; kill (Gate 3) → killed; void (admin) → killed. `app/api/void.py`
spends a 24-line docstring distinguishing them — the tell that one concept has
four names.

**D. deepen / reopen are just "advance a settled run."** Both re-enter the
pipeline at a later position; they exist as separate endpoints only because
"position" isn't a first-class field.

---

## 2. The target model

A run is fully described by:

```
run = {
  position:   "s1" .. "s7"                 # furthest stage reached (exists today as current_stage)
  target:     "s1" .. "s7"                 # how far the PM wants to go (= the chosen depth)
  lifecycle:  "running" | "paused" | "done"
  outcome:    "completed" | "stopped"      # set when lifecycle=done
  reason:     free text                    # why it stopped (auto-triage / PM / admin / failure)
}
```

> **Surfacing note (2026-07-03):** the *goal* is surfaced as **`depth`** (the human
> name: archive/note/structure/evaluate/decide), not a separate `target` field —
> `target` is depth's position 1:1 (archive→s1 … decide→s7), so exposing both would
> re-introduce a "same fact, two names" pair. `target` remains only as the
> `/decision` **request** input (`advance_to {target: <depth>}`) and for internal
> position inference; the run **response** carries `depth` + `position` (current
> location) + `lifecycle`/`outcome`/`reason`.

Everything in §1 derives from these:

| Today | Derived from |
|---|---|
| depth / mode | `target` position |
| status `waiting_direction/approval/routing_review` | `lifecycle=paused` + `position` |
| status `completed` | `lifecycle=done, outcome=completed` |
| status `killed` | `lifecycle=done, outcome=stopped` (+ `reason`) |
| status `failed` | `lifecycle=done, outcome=stopped, reason=error` |
| ended_by (noted/structured/…) | `outcome=completed` + `position` |
| ended_by (rejected/voided/kill_*/auto_triaged) | `outcome=stopped` + `reason` |
| head artifact type | `position` |
| approval actions direction/approve/confirm/override | one **advance** decision |
| approval actions reject/void/kill-confirm | one **stop** decision |
| deepen / reopen | **advance** from a done/paused run |

### One decision endpoint

The three gate endpoints + void + reopen + deepen collapse to:

```
POST /runs/{id}/decision
{ "action": "advance" | "advance_to" | "revise" | "stop",
  "target": "s7",            # for advance_to (= pick depth / deepen)
  "reason": "…" }            # for stop (= reject / kill / void), or advance note
```

- **advance / advance_to** — run stages from `position` up to `target`, pausing at
  the next checkpoint. Covers: Gate 1 direction, Gate 2 approve, Gate 3
  confirm/override, deepen, reopen.
- **revise** — re-run the current position with PM feedback (Gate 2 revise);
  `position` unchanged.
- **stop** — finalize `outcome=stopped` with `reason`. Covers: reject, Gate 3 kill,
  void, archive-as-set-aside.

A **gate** becomes a `pause: true` flag on a position. Today's checkpoints are
after S2, S4, S5; in the new model those are the only positions with `pause`. The
pipeline engine is one loop — "run stage, if position.pause then set
lifecycle=paused and return; else continue to target" — replacing the
archive/note/structure/evaluate/decide if-elif chain in
`runs.py::_continue_after_direction`.

### Routing, under the new model

`prd/poc/kill` stops being a fourth vocabulary. At position S5 the deterministic
rule (see `04-scoring.md`) chooses the **next position**: kill = `stop`, poc =
advance to S6A, prd = advance to S6B. The LLM's S5 rationale then *explains the
computed advance* instead of free-lancing a conclusion the rule contradicts —
which structurally fixes the rationale-vs-routing incoherence that the
`blocking→poc` change only patched at the value level.

---

## 2.5 The stage registry — one canonical name per position

Names are **consolidated, not lost.** Today a single stage is named by several
vocabularies at once — S3 is `structure` (depth), `opportunity_memo` (artifact),
`structured` (ended_by), and "Opportunity Creation" (stage) — the same position in
four words. The redesign defines each position **once**, in a single stage
registry that everything else derives from:

| position | stage name | artifact label | stop-depth¹ | ended_by (if completed here) | pause (gate) |
|----------|------------|----------------|-------------|------------------------------|--------------|
| s1  | Signal Ingestion         | Signal Summary      | archive   | archived   | — |
| s2  | Insight Extraction       | Insight Memo        | note      | noted      | ✔ (was Gate 1) |
| s3  | Opportunity Creation     | Opportunity Memo    | structure | structured | — |
| s4  | Persona Evaluation       | Evaluation Brief    | evaluate  | evaluated  | ✔ (was Gate 2) |
| s5  | Prioritization & Routing | Decision Memo       | —         | —          | ✔ (was Gate 3) |
| s6a | PoC Plan                 | PoC Plan            | —         | —          | — |
| s6b | PRD                      | PRD                 | —         | —          | — |
| s7  | Executive Summary        | Executive Summary   | decide    | decided    | — |

¹ **stop-depth** = the legacy depth name for "stop at this position". Only the five
stop positions (s1–s4, s7) are valid `target`s; s5/s6a/s6b are intermediate steps
of a `decide` run, never a stop point (so no depth / no completion `ended_by`).

This one table owns the stage **name**, the artifact **label/type**, the
depth-name, the completion `ended_by`, and the gate location. The `RunMode`,
`ended_by`, and `ArtifactType` enums stop being independent definitions and become
**derivations** of it:

- `depths()` (the 5 stop-depths) = the registry rows with a `stop-depth`.
- `ended_by` on completion = the row's `ended_by` for the run's `position`.
- artifact naming = the row's `artifact label` — addressed by `position`, so the
  `?artifact_type=…` query (and its alias-hardening) becomes `?position=s3`.
- The `checkpoint` artifact is **not** in the registry — it duplicates the stage
  output and is dropped.

Human-readable "depth" is expressed as **"up to \<stage name\>"** (target=s3 →
"up to Opportunity Creation"); a short label (`note`/`structure`/…) may be kept as
one optional column here for UI, never re-declared in a separate enum.

Implementation note: this registry is the **first build step** (`app/pipeline.py`)
— the single source of truth the current scattered maps (`app/modes.py` `MODES`,
`run_finalizer._ENDED_BY_FROM_MODE`, per-stage `artifact_type=` literals) derive
from, proven equivalent by tests before any schema cutover.

## 3. What this removes

- 3 status `waiting_*` values → 1 (`paused`).
- 11-value `ended_by` → `outcome` (2) + `reason` (text); no enum to maintain.
- `RunMode` (5) → `target` position (already an S-string).
- 6 endpoints (`/direction`, `/approve`, `/revise`, `/reject`, `/routing-review`,
  `/void`, `/reopen`, `/deepen`) → 1 (`/decision`).
- 3 `send_gate1/2/3` + 2 review builders → 1 generic "render paused run at
  position P" payload.
- `void.py`'s 24-line disambiguation → gone (stop+reason covers it).
- The `_continue_after_direction` branch chain → one stage loop.

No user-facing capability is removed. A run can still stop at any stage; the three
human checkpoints still exist; prd/poc/kill still happen. This is a
**re-encoding**, not a capability change — which is why it needs no usage data to
justify (unlike the capability questions in §6).

---

## 4. Migration / cutover

**Data: clean wipe (approved).** The 42 production runs are unvalidated dry-runs
(memory: `golden-set-not-ground-truth`, `synthetic-completed-runs-are-real`);
migrating them row-by-row (depth→target, status→lifecycle, ended_by backfill) has
near-zero value. Wipe instead. This removes the entire data-migration half of the
cost.

**Signal preservation (done / planned):**
- 24 of 32 signals are already durable in `WIKI_ROOT/raw/from-web/sensing/`
  (verified: 24/24 files present) — re-run by re-submitting from the wiki.
- The landmark manual signal (Android 16 MTE, `9b49fc8c`) exists only in the prod
  DB; pull it from prod at cutover time (before wipe) and re-submit. The other 7
  orphans are archive noise — dropped.

**Live consumers (the real cost — coupling measured 2026-07-02):**

*Observatory* (`code/core/jackhpark-pm-observatory`, Next.js) — reads the engine
SQLite directly + the API. Bounded to the adapter/format layer:
- `lib/adapters/engine-db.ts` — direct reads of `workflow_runs`, `signals`,
  `approval_events`, `stage_outputs`, `artifacts`, `run_batches`; references
  `status`, `routing`, `current_stage`, `waiting_direction`.
- `lib/format.ts` (~L91–115) — status → human-label map.
- `lib/pipeline-stages.ts` (L8–11) — `waiting_*` → pipeline-stage map.
- `components/GateActions.tsx` — the per-gate action buttons → one decision control.

*Hermes* (`ai-assets/jackhpark-hermes-control-plane`, Python) — polls the API:
- `distributions/hermes-ops/skills/gate-watcher/` — polls `status=waiting_*` and
  delivers per-gate prompts → poll `lifecycle=paused`, render generically by
  position (this simplifies Iris too — one queue, one renderer).
- `hermes_eval/` (`detect_alerts.py`, `render_digest.py`, `build_snapshot.py`) —
  consume `status`/`routing` for digests/alerts.

**Sequencing (one coordinated cutover, no dual-read window needed since data is
wiped):**
1. Engine: new schema + `/decision` endpoint + stage-loop; delete old enums/routes.
2. Observatory: update the 3 lib files + GateActions to read `position`/`lifecycle`.
3. Hermes: update gate-watcher poll + render, and `hermes_eval` status reads.
4. Deploy together; wipe DB; re-submit preserved signals.

Estimated as a **bounded multi-repo cutover** (adapter/format layers + one
gate-watcher skill), not the 1–2 week data migration originally feared. Exact
sizing pending a line-level read of `engine-db.ts` and the gate-watcher skill.

**Step 5 is staged "derive-first" (shipped in `app/run_view.py`).** The target
vocabulary — `lifecycle` / `position` / `target` / `outcome` / `reason` — is first
exposed as a *derivation* over the current `status` / `mode` / `current_stage`
fields and surfaced on the run object. This lets `/decision`, the observatory, and
Hermes migrate their reads to the new names *before* the storage changes. The
physical flip (dropping `status`/`mode`, making these real columns, and no longer
clearing `position` on finalize) is then the mechanical final act of the same
coordinated cutover — it swaps the storage under an already-adopted contract. A
bridge, not a permanent shim: `run_view.project()` collapses into the store at
cutover.

**The coordinated cutover itself is an executable runbook: [CUTOVER_step6.md](CUTOVER_step6.md)**
— the exact engine / observatory / Hermes edits (grounded in a line-level read of
all three repos), the wipe + reseed procedure, deploy order, rollback, and a smoke
test. It needs no new design decisions; it executes this model.

---

## 5. Backward-compatibility stance

Clean break. No `_missing_` alias hooks, no legacy status coercion — the three
existing alias layers (`file/brief/opportunity`, `awaiting_direction`, signal
`pending`) are dropped, not carried forward. The wipe + coordinated cutover is
what makes this affordable; a compat shim would re-introduce the parallelism this
redesign exists to remove.

---

## 6. Deferred *capability* decisions — now trivial in the new model

These were the data-gated questions (E5 US-53/54). Under (position, lifecycle) each
is a small config choice, not a restructure — decide them anytime, with or without
more data:

- **Drop `evaluate`** — stop offering S4 as a `target`/stop position.
- **Merge Gate 2 + Gate 3** — make S4 the only `pause` position in decide mode;
  pre-compute the S5 routing and show it at the S4 pause. (The first prod decide
  run showed Gate 3's human override earned its keep, so hold this until more
  decide runs exist.)
- **Reclassify `archive`** — it is just `stop` at position S1; no special value.

---

## 7. Open questions

- **Fan-out / batch** (`batch_id`, `run_batches`, portfolio synthesis) and **retry
  lineage** (`attempt_no`, `root_run_id`) are orthogonal to (position, lifecycle)
  and carry over unchanged. Confirm no hidden coupling to the status vocabulary.
- **`revise` semantics** — re-running a position with feedback needs a version
  bump on that position's stage output (S4 already versions; generalize).
- **Observatory historical views** (retry lineage, integrity checks in
  `lib/integrity.ts`) — confirm they survive the schema change or are dropped with
  the wiped data.
- **Exact cutover size** — pending line-level reads of `engine-db.ts` and the
  gate-watcher skill.
