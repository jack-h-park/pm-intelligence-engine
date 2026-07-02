# Reverse Roadmap
## Remaining Gap Closure Backlog

**Version:** 1.2  
**Last updated:** 2026-06-10 (v1.2: refactor deployed to iMac; US-42 Blocking verifier built opt-in, pending eval validation)

This document contains only work that remains after the current verified baseline.
Implemented workflow, contracts, and tests are tracked in:

- [Reverse PRD](PRD_REVERSE.md)
- [Implementation Status](IMPLEMENTATION_STATUS.md)

---

## Now

### E5 — Depth-ladder & terminal-state simplification (data-gated)

This epic records a **structural review** of the depth ladder and gates
(2026-07-01/02) and — importantly — what was *deliberately not changed*. The
review was a diagnosis; only the reversible, additive parts were shipped. The
destructive simplifications are **deferred behind a data check**, not rejected.

> **What a PM/maintainer must know:** the pipeline a PM experiences today is
> unchanged except for one added "go deeper" action and a more conservative S2
> suggestion. The 5-depth ladder (archive<note<structure<evaluate<decide) and the
> three gates are **intact**. The bigger merges below are decisions *scheduled for
> ~2026-08*, to be made against real usage, not now.

**Motivating data (prod DB, 5 weeks / 42 runs, checked 2026-07-01):** `decide`
never chosen; Gates 2/3 never fired; `routing` NULL on every run. Gate-1 depth
picks: archive 6, note 5, structure 5, evaluate 1, decide 0. The PM downgraded
S2's suggestion in 9/17 decisions and never upgraded — the ladder is *effectively*
3-deep and the upfront depth choice biases shallow. See memory
`prod-usage-decide-never-used`.

#### US-51 — Deepen: depth as an incremental pull (shipped, the experiment)
**Status:** ✅ Shipped (2026-07-02, PR #28); first production use = run `9b49fc8c`
(Android 16 MTE), which reached the first-ever prod Gate 2.

Instead of merging depths, make going deeper *cheap and reversible* so the Gate 1
choice stops being a high-stakes upfront forecast:
- `POST /runs/{id}/deepen {depth}` resumes a completed run at a strictly deeper
  depth, **reusing stored S2/S3/S4 outputs** — only the added stages run
  (`evaluate→decide` reaches Gate 2 with zero LLM calls). Records a `deepen`
  calibration event (`from=X; to=Y`).
- S2 suggestion calibrated conservative (tie-break shallower) to match the 9/17
  downgrade pattern.

**Why this instead of the merges:** it gathers the missing data (do PMs deepen?
does anything reach decide?) *without* discarding structure we might want. If, in
~1 month, `deepen`-to-decide is used and produces value, the ladder earned its
depth; if evaluate/decide stay at zero even with deepening free, the merges below
become well-evidenced.

#### US-53 — Depth-ladder merge (DEFERRED — decide ~2026-08 on US-51 data)
**Status:** ⏸ Deferred, criteria set. Do **not** implement before reviewing:
`GET /runs?event=deepen` usage + evaluate/decide completion counts.

Candidate simplifications, each with its trigger:
- **Merge `structure`+`evaluate`** if `evaluate` selections stay near-zero (the
  only difference is whether S4 runs; PMs don't seem to want "evaluated but not
  routed" as a stopping point).
- **Drop `note`'s S7 call** — `note` re-runs S7 to produce a half-"not run"
  summary while S2 already persisted `insight_memo`. Define note = "S2 insight
  memo is the artifact"; removes one stage + one LLM call. (Low-risk; can ship
  independent of the merge decision.)
- **Reclassify `archive` as an outcome, not a depth** — it is "set aside", which
  is why `void.py` had to be invented to distinguish an improperly-started run
  from evaluated `archive` work. The new `ended_by` column (US-52) is the first
  step: it already separates `auto_triaged`/`archived`/`voided` without touching
  the depth enum.

#### US-54 — Gate 2 / Gate 3 consolidation (DEFERRED — revisit after US-51)
**Status:** ⏸ Deferred. Supersedes the framing of DESIGN_DECISIONS §4.

In `decide` mode the PM approves at Gate 2 (post-S4), then re-confirms routing at
Gate 3 (post-S5) over largely the same persona/composite data. S5 is
deterministic, so Gate 2 could pre-compute "will route to prd" and offer
approve+confirm (with override) in one stop. **Blocked on evidence:** we have not
yet observed a single run traverse Gate 2→3 in production (9b49fc8c is the first).
Revisit once a few real decide runs show whether the second gate earns its
friction; premature merge would remove a control we have never exercised.

#### US-52 — Terminal-state legibility & low-cost cleanups (in progress)
**Status:** 🚧 In progress (this PR). Safe, additive items from the review:
- **`ended_by` column** (shipped here): one terminal-reason token stamped at
  finalize (`auto_triaged`/`archived`/`noted`/`structured`/`evaluated`/`decided`/
  `rejected`/`kill_confirmed`/`kill_overridden`/`voided`/`failed`). Collapses the
  `status`+`mode`+`routing`+`approval_events` join the observatory otherwise needs
  to answer "how did this run end?" Addresses the 2026-06-21 untraceable-kill
  incident (8 kills with no recorded reason). Nullable; legacy rows stay NULL.
- **`artifacts?artifact_type=` hardening** (shipped here): unknown types now 422
  at the edge instead of 500 deep in the store; depth-named guesses
  (`structure_memo`→`opportunity_memo`) are aliased.
- **Doc de-drift** (shipped here): CLAUDE.md corrected (stage prompts are
  shared/product-agnostic, not per-product); ARCHITECTURE.md notification
  ownership already fixed under US-50.

**Deferred within US-52 — need owner confirmation (external consumers):**
- Remove `POST /runs/{id}/scan` (fan-out to all) in favour of `/promote`
  (per-product) — Hermes may still call `scan`; verify before removing.
- Stop creating a `run_batches` row for single-product manual starts; close the
  16 orphaned open batches — observatory reads `run_batches` directly.
- Unify `DECISION_CONTEXT_ROOT` / `DECISION_SYSTEM_ROOT` (identical value, two
  names) — internal-only refactor, ~6 files; cosmetic, low-risk, unscheduled.
- Retire the legacy aliases (`file/brief/opportunity`, `awaiting_direction`,
  signal `pending`) — only after confirming Hermes/observatory no longer emit
  them; set a removal date rather than ripping out the `_missing_` hooks now.

### E4 — Hermes Handoff Completion

#### US-20b — Auto-triage archive ownership cutover
**Status:** In transition

As a maintainer, I want Hermes to own the auto-triage wiki archive fully so
pm-engine has no remaining wiki write responsibility.

**Acceptance criteria**

- Hermes archives `status=completed` + `mode=file` runs into
  `raw/from-pm-decision-context/kills/auto-triaged/`
- `AUTO_TRIAGE_LOCAL_ARCHIVE_ENABLED=false` is supported and documented
- pm-engine completion remains successful with local archive disabled
- legacy local archive call path can be removed after Hermes validation

#### US-50 — Notification delivery consolidation: single owner + channel policy
**Status:** ✅ Completed (2026-07-02). Audit of the live gate-watcher showed the
Iris side was further along than assumed: dedup (`gate-notified.json`,
`<run_id>:<status>` + `run_updated_at`, 24h staleness re-nag, terminal
notify-once) and terminal-result messages (incl. per-signal batching and
PM-chosen-archive suppression) were already implemented. The real channel policy
is **origin-affinity routing** (deliver to the platform+channel the run started
on via `run-chat-map.json`; fallback `GATE_NOTIFY_DEFAULT_CHAT_ID`) — the
contract §3 was rewritten to record that verified policy instead of the assumed
fixed class→channel map. Closing work: engine exposes `review_url` on run
objects (Gate 2 messages can carry the review link without Iris knowing the
engine's network config); gate-watcher skill + SOUL.md + control-plane skill
updated on the iMac to include the review link and the `deepen` relay rule.

Found 2026-07-02 while verifying the first production Gate 2 (the US-48 cutover
worked — Iris delivered the prompt to Discord — but the surrounding surface was
incoherent): gate prompts arrive on **Discord**, reconciler/expiry digests on
**Telegram**, the engine's dormant `FanoutNotifier` was one flag-flip from
re-creating a duplicate delivery path, and `ARCHITECTURE.md` still claimed the
engine owned notifications ("Hermes does not participate in the notification
loop"). No single document said who sends what, where, or how duplicates are
prevented — the PM experienced this as notifications being "all over the place".

**Engine side (done 2026-07-02):**
- `docs/NOTIFICATION_CONTRACT.md` — normative ownership table, the engine
  interface Iris consumes (gate queues, review payloads, event filters), the
  dedup key `(run_id, status, updated_at)`, and the 3-class channel policy
  (A: decision-required prompts / B: informational digests / C: ops alerts —
  one channel per class).
- `ARCHITECTURE.md` de-drifted: all "pm-engine fires notifications directly"
  claims replaced with the post-US-48 reality (7 locations incl. §11 rewrite).
- `notifier.py` marked LOCAL/DEV-ONLY in its module docstring with an explicit
  "do not flip the flag in prod" warning.

**Iris side (remaining — hermes control-plane repo):**
- Record/confirm the Class A channel and consolidate B/C per the contract table
- Dedup keyed on `(run_id, status, updated_at)` if not already
- Terminal-result messages for non-decide completions (US-48 folded scope)
- Gate 2 messages carry the review-page link

**Acceptance criteria**
- Every production message class maps to exactly one channel, recorded in
  `NOTIFICATION_CONTRACT.md` §3; changing a channel updates the doc in the same
  change
- No transition is announced by two systems (dedup tuple observable in Iris logs)
- `GATE_NOTIFICATIONS_ENABLED` remains false on the iMac; docs and code agree
  on ownership

### E5 — Quality Calibration

#### US-23b — Eval calibration for active model baseline
**Status:** ✅ Completed

As a maintainer, I want eval outcomes calibrated for the active model baseline
so routing regressions are distinguishable from prompt/model drift.

**Acceptance criteria**

- choose and document the official eval model baseline
- explicitly document model-specific variance where it exists
- document whether production/eval should prefer Claude or OpenAI
- keep `eval/scenarios.json` aligned with the chosen baseline

**Resolution**

- Eval execution is model-agnostic and uses the active `LLM_PROVIDER`
- `eval/runner.py` now prints the runtime provider/model explicitly so results are attributable
- `gpt-4o` routing drift on R04/R07 remains documented as model-specific variance

### E7 — Decision Quality Refactor (2026-06)

Sequential refactor decided 2026-06-10 after a full implementation review.
Stories below are **strictly ordered** — each is independently shippable, and
`python eval/runner.py` must pass before moving to the next.
Background and rule rationale: `docs/DESIGN_DECISIONS.md § 8` and
`pm-decision-context/core/04-scoring.md` ("Routing Decision — Two-Axis Hybrid Rule").

#### US-27 — LLM output robustness (Step 1)
**Status:** ✅ Completed (2026-06-10, commit 4099391)

As a maintainer, I want malformed LLM JSON output to be repaired via bounded
retry instead of failing the run, so downstream refactor steps can be validated
without flaky failures.

**Acceptance criteria**
- Shared parse path: on `json.JSONDecodeError`, re-prompt with the parse error
  and raw output, max 2 attempts, then fail with the existing semantics
- Applies to all LLM-calling stages (S2–S7) and S4 agents
- Repair attempts are visible via emitted events

#### US-28 — Eval golden set boundary cases (Step 2)
**Status:** ✅ Completed (2026-06-10, commit db7daee)

As a maintainer, I want boundary scenarios in `eval/scenarios.json` before the
routing rule changes, so US-29 is verified by measurement, not inspection.

**Acceptance criteria**
- Scenarios covering: composite just above/below the PRD threshold,
  Confidence 3 vs 4 at high composite (the case the hybrid rule exists for),
  blocking-assumption kill, and the auto-triage relevance boundary
- Expected values derived from the two-axis hybrid rule (these scenarios are
  EXPECTED TO FAIL until US-29 lands — document this in the scenario notes)

#### US-29 — Two-axis hybrid routing (Step 3)
**Status:** ✅ Completed (2026-06-10, commit 2e5b199 — live eval: R05/R06/R07 pass; R04 fails on documented gpt-4o blocking-classification drift, pre-existing at 2026-05-24 baseline)

As the PM, I want routing to use composite for kill/PRD-eligibility and
Confidence for PRD-vs-PoC, so unvalidated opportunities cannot skip validation.

**Acceptance criteria**
- `_compute_routing()` implements: blocking or composite ≤ kill_threshold →
  `kill`; composite ≥ prd_threshold and confidence ≥ confidence_gate → `prd`;
  composite ≥ prd_threshold and confidence < confidence_gate → `poc`; else `poc`
- Thresholds (defaults 1.5 / 3.5 / 4) read from `products/<id>/scoring.yaml`
  with the same fallback pattern as dimension weights
- R04–R07 golden scenarios still pass; US-28 boundary scenarios now pass
- Remove the "Implementation status" pending note from
  `pm-decision-context/core/04-scoring.md`

#### US-30 — Gate 3 information enrichment (Step 4)
**Status:** ✅ Completed (2026-06-10, commit 7eec9f8)

As the PM, I want the Gate 3 notification and run payload to include the full
assumption list (not just a blocking count), per-persona one-line summaries,
and the S4 rubric score, so I can confirm or override routing without opening
the database.

**Acceptance criteria**
- `GET /runs/{id}` (and Gate 3 notification) expose assumptions with severity,
  persona score+argument summaries, and rubric total
- Additive API change only — hermes-ops Gate 3 skill keeps working unmodified,
  then can be updated to render the richer payload

#### US-31 — Auto-triage safety net (Step 5)
**Status:** ✅ Completed (2026-06-10, commit 14d5bd4)

As the PM, I want auto-triaged (silently filed) signals to be reviewable and
revivable, so a miscalibrated S2 relevance score cannot permanently discard a
signal without any human ever seeing it.

**Acceptance criteria**
- Auto-triaged runs are queryable: `GET /runs?event=auto_triaged&since=...`
  (or equivalent filter)
- `POST /runs/{id}/reopen` revives an auto-triaged run to `awaiting_direction`
- Digest delivery is **Hermes-owned** (consistent with the polling pattern from
  the notification_chat_id revert): hermes-ops or hermes-eval polls the query
  endpoint and includes an auto-triage section in its daily digest; pm-engine
  sends no new notifications

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
- decide whether retry/backoff belongs in pm-engine or Hermes monitoring

### E5 — API Consumer Ergonomics

#### US-17c — Artifact endpoint adoption validation
**Status:** ✅ Completed

`GET /runs/{id}/artifacts` implemented in `app/api/artifacts.py`, registered in
`main.py`, backed by `list_artifacts()` in store protocol + SQLiteStore, and
integration-tested in `tests/integration/test_artifacts_api.py`.
`GET /runs/{id}?include_outputs=true` retained for stage-level inspection.
Further artifact endpoints deferred until Hermes integration confirms need.

### E7 — Decision Quality Refactor (continued)

#### US-37 — Persona prompt ownership migration (Step 6)
**Status:** ✅ Completed (2026-06-10, pm-engine af3269d / decision-context afdfd10)

As a maintainer, I want the S4 persona prompts (question sets, lens
descriptions, instructions) to live in `pm-decision-context` templates instead
of being hardcoded in `app/agents/*.py`, so the "workflow design lives in
decision-context, engine automates it" ownership rule holds and persona prompts
cannot silently drift from `03-persona-system.md` (the same failure class as
the routing v2 doc/code mismatch).

**Acceptance criteria**
- Persona lens/question/instruction text loaded via `template_service` from
  `pm-decision-context/prompts/` (per-persona template or structured sections)
- `app/agents/*.py` retain only persona/dimension/weight wiring
- Eval golden scenarios pass unchanged (prompt content identical at migration)
- Ordered **before** US-32 so all subsequent prompt work happens in one repo

#### US-32 — Calibration anchors + traceable principles (Step 7)
**Status:** ✅ Completed (2026-06-10). Deliverables landed: S5 Blocking/Informing worked examples (R06/R04), S2 relevance anchors, Explorer reach bullet, S5 `governing_heuristics` field. **Open finding (see US-39):** anchors did not close the gpt-5.4 over-flagging gap.

As the PM, I want stage prompts calibrated against the golden runs and
decisions traceable to my stated principles, so scoring is consistent over time
and consistency is auditable.

**Acceptance criteria**
- S2/S4/S5 prompts carry few-shot anchors from archived golden runs (R01–R07):
  "relevance 2 looks like R0x; relevance 4 looks like R0y", plus a worked
  Blocking-vs-Informing example from R06
- Explorer (Impact) question set gains a **reach quantification** sub-question
  ("what is the current footprint — which segments, what scale, today?") so
  Impact covers reach × magnitude, not only expansion ceiling
  (from the 2026-06-10 RICE review: reach was "absorbed into Impact" on paper
  but absent from the actual prompt)
- S5 decision record gains a **governing heuristics** field — rationale cites
  the applicable decision heuristics from `00-pm-identity.md` by number
  (e.g. "Governing heuristics: #7, #14"), making the
  philosophy → principles → execution chain auditable per decision and feeding
  the calibration log
- Eval measures anchor effect: golden scenarios still pass; score variance
  across repeated runs is compared before/after

#### US-33 — Wiki precedent injection (Step 8)
**Status:** Open (blocked by US-32)

As the PM, I want past decisions and durable concepts to inform new
evaluations, closing the wiki feedback loop:

- `context_loader` gains a fourth context layer injecting kill-pattern taxonomy
  + calibration heuristics into S5, and similar-signal outcomes into S2.
  v1 may load whole per-product case files; retrieval/selection comes later if
  token size demands it
- S3 output gains an optional **`candidate_durable_concept`** field (JTBD
  method steps 3–4: name the durable abstraction behind the opportunity);
  run export carries it so wiki ingest can propose additions to
  `wiki/concepts/` — making the conceptual-model layer compound across runs
  instead of being consumed per-run

#### Explicitly rejected (2026-06-10)

- **Portfolio/ranking view (cross-run prioritization):** the workflow is
  sensing-triggered and evaluates one signal at a time; execution ordering of
  resulting PRDs/PoCs belongs to the roadmap process, not pm-engine.
  "Comparability" is redefined as temporal calibration in
  `pm-decision-context/core/04-scoring.md` (Purpose § 2) and is served by
  US-32 anchors, not by a ranking feature.

---

## Later

### E6 — API Authorization Hardening

#### US-26 — Per-profile action scoping (P1)
**Status:** Planned (depends on P0 bearer auth being stable in production)

As a maintainer, I want each Hermes profile to be restricted to the pm-engine
endpoints it is authorized to call, so that a compromised or misconfigured
profile cannot approve runs or modify state beyond its declared scope.

**Design doc:** `hermes-control-plane/docs/design/guardrail-enforcement-roadmap.md § P1`

**Acceptance criteria**

- pm-engine supports per-token (or per-caller) action scopes; requests to
  endpoints outside the caller's scope return `403`
- `distributions/*/mcp/pm-engine.yaml` `approved_actions` lists become the
  source of truth for each profile's token scope
- read-only profiles (e.g. `eval`) cannot call `POST /runs/{id}/approve`
- write profiles (e.g. `ops`) retain full access

### E8 — Evaluation Depth (backlog, direction fixed 2026-06-10)

#### US-40 — Rename assumption severity Informing → Adjusting + canonical definition
**Status:** ✅ Completed (2026-06-10). The Blocking/Informing vocabulary was
unintuitive and lossy (PM feedback). Renamed Informing → **Adjusting** across
engine (`Assumption.severity` Literal + non-destructive legacy validator),
prompts, and wiki. Canonical "Blocking vs Adjusting" definition (whether-vs-how,
two-question test, worked example, transient-case caveat) now lives once in
`decision-context/core/04-scoring.md`; S5 prompt + wiki reference it. Wiki concept
file renamed (blocking-vs-informing → blocking-vs-adjusting, 10 links updated) and
its stale routing fixed (Blocking → Kill). Future work **US-41 (Part B)**: add a
time-horizon/magnitude dimension so a *transient-but-large* opportunity can route
to a fast time-boxed bet instead of Kill.

#### US-42 — Adversarial Blocking-assumption verifier
**Status:** Implemented, opt-in, **unvalidated** (2026-06-10, commit 504aa0e).
After S5 classifies assumptions, an optional second adversarial LLM pass
re-applies the strict two-question test to each Blocking and downgrades to
Adjusting when a plausible alternative path exists — correcting gpt-5.4's
over-eager Blocking flags (false Kills). Binary alternative-path test only; the
time-horizon/magnitude axis is US-41 (Part B).

`BLOCKING_VERIFIER_ENABLED` defaults **False**. Before enabling in production:
- **Validation gate (open):** run a gpt-5.4 eval confirming the verifier
  preserves genuine Kills (R06's three real blockings stay Blocking) while
  downgrading over-flags (R04/R05). This session's eval run hit OpenAI rate
  limits before reaching S5 — measurement deferred.
- Risk if wrong: over-downgrading turns genuine Kills into proceed. Hence opt-in.

#### US-39 — Production model calibration: gpt-5.4 kill-bias
**Status:** Decided 2026-06-10 — keep gpt-5.4; retire the old golden set as a target

Production (iMac) runs `LLM_PROVIDER=openai` / `gpt-5.4` — **decided as the
production model** (cost/performance/use-case tradeoff). gpt-5.4 systematically
classifies more assumptions as Blocking than the old Claude dry-runs did.

**Decisions:**
- **Model:** gpt-5.4 stays. Pin it for both production and any calibration so
  there is no cross-model conflict. (Option A "switch to Claude" is OFF.)
- **Golden set:** R01–R07 are unvalidated dry-runs, **not ground truth** — so
  "gpt-5.4 is wrong vs the baseline" is an invalid frame. `eval/scenarios.json`
  is to be relabeled as regression fixtures; the real calibration target is
  **accumulated human Gate decisions** going forward. (See memory:
  golden-set-not-ground-truth.)

**Remaining (open) — the kill-bias itself still wants a real fix:**
- **US-42 verifier pass** — built (opt-in, unvalidated); a second adversarial
  call challenges each Blocking before it counts toward Kill. Pending eval
  validation, then enable.
- **US-41 (Part B)** — enrich the decision model (time-horizon/magnitude) so
  transient-but-large opportunities aren't force-killed.

#### US-41 — Value Horizon: transient-but-large opportunities (Part B)
**Status:** ✅ Implemented 2026-06-10. Canonical design:
`pm-decision-context/core/04-scoring.md` → "Value Horizon".

Blocking/Adjusting captures dependency but not *time*. A transient-but-large
opportunity (real value now, but a vendor may close the window) fits neither
"Adjusting → slow PRD" nor "Blocking → Kill"; the right move can be a fast,
time-boxed bet. Adds a second axis (value horizon × magnitude), orthogonal to
Blocking/Adjusting.

**Agreed design (human-in-the-loop, minimal — no automatic 4th route):**
1. **Detect** — opportunity carries `value_horizon: durable | transient`
   (LLM-judged), set in **S3**, available at **S5**.
   - `S3OutputData` gains an optional `value_horizon` field (default "durable").
2. **No new route** — routing stays `prd | poc | kill`.
3. **Magnitude = existing Impact score** (no separate sizing).
4. **Surface at Gate 3** — when S5 sees `value_horizon == transient` AND high
   Impact AND not Blocking, attach a Gate 3 recommendation: "⏳ Closing window —
   consider a time-boxed bet (fast/minimal PRD) over the default track." The PM
   confirms the default or overrides to a fast-tracked PRD.

**Acceptance criteria**
- S3 prompt + `S3OutputData.value_horizon` (optional, default "durable",
  backward-compatible like `governing_heuristics`)
- S5 computes a `closing_window` recommendation (transient + Impact ≥ threshold +
  no Blocking) and includes it in the Gate 3 payload (`gate3_review`) and
  notification — additive, no routing-logic change
- Tests: tag flows S3→S5; recommendation fires only on transient+high-Impact+
  no-Blocking; durable opportunities unaffected
- R04 worked case: flagged as closing-window at Gate 3

#### US-34 — LLM-as-judge rubrics (replacing keyword-matching rubrics)
**Status:** Backlog — direction agreed, implementation not scheduled

The keyword/word-count rubrics in `eval/rubrics/` (grounding via keyword
inclusion, skeptic quality via phrase blacklist) are form checks, not quality
checks. Replace them with LLM-as-judge calls, offline first, runtime later.

**Alignment with hermes-eval (agreed direction):**
- **Judgment lives in pm-engine only.** The judge prompts/rubrics are part of
  the engine's eval harness (and later its runtime quality gate). hermes-eval
  never implements or runs its own judge — one source of judgment, no drift
  between two evaluators.
- **hermes-eval stays a read-only observer** (per its SOUL: "do not change what
  you measure"). It consumes rubric scores the engine has already persisted
  (stage outputs / artifacts via `GET`), and extends its daily digest with
  quality trend lines: rolling S4 rubric average, Gate 3 override rate,
  auto-triage rate vs the 10–20% target kill rate, judge-score drift alerts.
- **Scheduling stays in Hermes, execution stays in pm-engine:** periodic golden
  set regression (`python eval/runner.py`) may be triggered on the hermes-eval
  schedule, but it runs as the engine's harness; hermes-eval only reports the
  result file.
- This division is consistent with US-26 per-profile scoping (eval profile =
  read-only token) and requires no new write surface.

#### US-35 — S4 synthesis round (optional 5th call)
**Status:** Backlog

After the four independent persona evaluations, a synthesis call distills the
points of disagreement into a contested-issues summary, so Gate 2 presents
"what the personas disagree on" instead of four raw score cards. Persona
independence (bias control) is preserved — synthesis runs strictly after.

#### US-36 — Conditional Gate 3 auto-confirm
**Status:** Backlog — deliberately deferred

Auto-confirm routing when no blocking assumptions exist and composite is far
from all thresholds, with notify-and-timeout semantics. Trades against the
human-in-the-loop principle; revisit only if gate fatigue is observed in
practice.

#### US-44 — Decision-audit completeness (all human gate decisions persisted)
**Status:** ✅ Completed (2026-06-11). Foundational for "human Gate decisions =
the real calibration dataset" (see memory: golden-set-not-ground-truth).

Previously only Gate 2 (approve/revise/reject) was recorded in `approval_events`;
**Gate 1 (mode) and Gate 3 (routing) decisions were not persisted** — 2/3 of the
human decisions were missing from the audit log. Now every gate decision is a
labeled datapoint capturing system-suggestion vs PM-choice:
- Gate 1 `direction`: `chose=<mode>; suggested=<s2_suggested>`
- Gate 3 `confirm`/`override`: `chose=<routing>; recommended=<s5_routing>[; reason=...]`
- queryable via `GET /runs?event=direction|confirm|override` (+ existing filters)

#### US-48 — Gate-notification cutover flag (consolidate notifications to Hermes)
**Status:** ✅ Cutover complete — verified in production 2026-07-02: flag false on
the iMac, Iris gate-watcher polling and delivering Gate 1/2 prompts (first live
Gate 2 was the deepened run 9b49fc8c). Follow-up consolidation (channel policy,
dedup, doc de-drift) tracked as US-50.

Notification ownership is consolidating to Hermes-ops (conversational, LLM-authored
messages) instead of pm-engine's fixed-template push. pm-engine keeps the gate
**state machine + API + queue queries**; it stops **composing human-facing messages**.

- `GATE_NOTIFICATIONS_ENABLED` (config) — **single chokepoint** in `build_notifier()`:
  when false, zero providers are wired so all send_gate1/2/3 calls are no-ops (the
  three call sites are untouched). Default **true** (local/test); set false on the iMac.
- **Cutover sequencing (avoid double- AND no-notification):** ship this flag (done,
  default true → no prod change); then in ONE maintenance window flip iMac to false
  AND enable the Hermes-ops `gate-watcher` cron sweep together. Reversible (flip + restart).
- **Terminal notifications** (the evaluate-run silence) are folded in: Hermes-ops will
  poll `GET /runs?status=completed|killed` and compose result messages — so pm-engine
  needs no completion-notification feature (replaces a would-be US-47). gate1_review /
  gate3_review payloads (US-46/30/41) are what ops reads to compose messages.
- Hermes side (build the ops gate-watcher + enable sweep) is separate hermes-control-plane
  work; handover reviewed 2026-06-11. Restart on iMac is via launchd
  (`launchctl unload/load com.jackpark.pm-engine`), not `make restart`.

#### US-46 — Gate 1 information enrichment
**Status:** ✅ Completed (2026-06-11). Found during the first live run: the Gate 1
notification showed only relevance + suggested depth + a one-line reason — too
thin to actually decide the processing depth. The S2 insight (what_changed,
reframing, relevance_explanation, pillar_references) was produced but never
surfaced. Same under-surfacing pattern as Gate 3 (fixed in US-30).

- `send_gate1` (Telegram/Slack/Fanout) now includes what_changed, why-it-matters,
  pillars, a depth-ladder reminder, and the `{"depth": ...}` command
- `GET /runs/{id}` gains a `gate1_review` payload (S2 insight + S1 summary +
  `suggested_depth`) — parallel to `gate3_review`
- additive; tests for payload + notification rendering; 239 pass

#### US-45 — Decision revisit / reversal (design needed)
**Status:** Backlog — design discussion required (found 2026-06-11)

There is no process to re-review or reverse a decision after a run reaches a
terminal state. Within a run: Gate 2 has a `revise` loop and Gate 3 has
`override`, but there is no cross-gate backward movement and no post-terminal
reopen for deliberately-decided runs (US-31 `reopen` is auto-triaged-only;
non-auto-triaged returns 409). A completed PRD/PoC or a killed run is final;
new information forces a brand-new run with no link to the superseded decision.
DESIGN_DECISIONS § 4's "the PM can reject the artifact" mitigation has no
endpoint behind it.

**Open design questions (need PM input before building):**
- Reopen a terminal run to *where* (awaiting_direction? the deciding gate?)
- What happens to already-produced/exported artifacts (PRD/PoC, archive export)?
- Supersede semantics — link a new run as the successor of a reversed decision?
- Should reversals themselves be recorded as decisions (they should — US-44 pattern)

#### US-43 — Processing-mode vocabulary + canonical documentation
**Status:** ✅ Completed (2026-06-11). Renamed the depth ladder for clarity —
`file→archive`, `brief→note`, `opportunity→structure` (evaluate/decide kept) —
with legacy-value normalization (RunMode `_missing_`, Pydantic before-validators,
API-input + DB migration) so stored data and existing clients keep working.
Canonical "Processing Depth — 5 modes" added to decision-context
`core/02-workflow.md`; engine `_MODE_GUIDANCE` refers there. **Part B:** the API
field was renamed `mode → depth` (the concept is a depth, not a behavioral mode);
`mode` is kept as a deprecated input alias and mirrored in responses (Hermes
unaffected). Internal DB column stays `mode` (impl detail).

The five processing modes (`file | brief | opportunity | evaluate | decide`)
are really a **progressive-depth ladder** (how far to process a signal: skip →
insight → frame → evaluate → decide+build), but two problems:

1. **Terms don't signal the ladder.** `file`/`decide` read OK, but `brief` is
   ambiguous (noun vs verb), and `opportunity` names a *stage/artifact* (S3
   output) rather than a depth/action — inconsistent with the action-like
   siblings, so the list doesn't read as an ordered escalation.
2. **No canonical definition; same ownership drift as the relevance table and
   old Blocking/Informing.** The mode definitions live ONLY in engine code
   (`s2_insight.py` `_MODE_GUIDANCE`, plus scattered Literals in
   `models/stages.py`, `direction.py`, `approvals.py`, `run_finalizer.py`).
   `pm-decision-context` (which owns workflow design) has no "processing depth /
   modes" definition — `02-workflow.md` documents stages, not modes.

**Acceptance criteria**
- Add a canonical "Processing Depth — 5 modes" section to `pm-decision-context`
  (e.g. `core/02-workflow.md`): the ladder, where each mode stops, and when to
  pick it. Engine `_MODE_GUIDANCE` references it (close the drift).
- (Optional, separate decision) rename for ladder clarity — e.g. `opportunity →
  frame`, `brief → note`. Cross-cutting (code Literal + prompts + hermes skills),
  so get PM sign-off on terms first, like the Informing → Adjusting rename (US-40).

Related drift instances: S2 relevance table (code-only), Blocking/Adjusting
(US-40). Pattern: workflow-design vocabulary buried in engine code instead of
decision-context.

---

## Exit Criteria

The remaining roadmap can be considered closed when:

- Hermes owns all wiki writes, including auto-triage archive
- eval outcomes are calibrated for the chosen model baseline
- artifact access is stable enough that Hermes does not need DB/file knowledge
- export behavior is explicit and observable under retries and collisions
