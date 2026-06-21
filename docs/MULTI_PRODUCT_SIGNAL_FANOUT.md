# Multi-Product Signal Fan-out (US-49)

Design for supporting **one signal → many products** (1:N), replacing the current
strict 1:1 signal-to-product binding.

Status: **Implemented (Phase 1a–1d), then revised — see §0. The 1:N *plumbing* stands;
the eager fan-out *policy* is superseded by a conservative default.**

---

## 0. Revision — conservative fan-out (supersedes the eager default)

The original design (§2–§6 below) fans a product-agnostic signal out to **every** product
that clears the Triage threshold, each running the full S2→S7 pipeline with its own gates.
Production data showed this is **net-negative**:

> Live engine DB (9 signals / 22 runs / 5 batches): **2.44× run amplification**, 67% of
> signals fanned out (half to the max width of 4). The funnel then collapsed at the S2→S3
> boundary — **S2 reached by 19 runs, S3 by 2, S5 (routing) by 0**. Nine runs sat stuck at
> `waiting_direction` (Gate 1); one batch had **4/4** siblings stuck. **Zero** routing
> decisions and **zero** portfolio syntheses were ever produced — batches never settled
> because siblings stalled at the gate.

So the fan-out paid full cost (S1/S2 + Gate 1 load ×2.4) for none of its benefit (per-product
decisions, the synthesis memo). The cause is **fanning out *before* Gate 1**: it mass-produces
runs that do S1/S2 then stall. Note too that Triage (one cheap all-product relevance call) and
S2 (per-product relevance) **duplicate the same judgement**, both *before* the cliff.

**New default — one primary product, human-pull promotion:**

1. **Triage is unchanged** — one cheap call scoring the signal against all product profiles.
2. **Spawn only the primary product** — the highest-relevance product (within the most-relevant
   **product family**, below). Other above-threshold products are recorded as **deferred
   candidates** on the batch; they are *not* run.
3. **Batch membership stays open.** Synthesis cannot fire on a single-run batch anyway.
4. **Promotion is the only way to add a product.** `POST /runs/batch/{batch_id}/promote
   {product_id, depth?}` creates a sibling run that **reuses the primary's S1 output, starts at
   S2** (S2–S4 are product-specific; S1 is not), and **skips Gate 1** (the promotion act carries
   the depth). It does *not* re-enter at Gate 0 (signal already admitted) nor at Gate 2 (no S4
   for that product yet).
5. **Closing the batch** (PM declines further promotion) re-enables the existing Variant 2
   synthesis trigger — now meaningful only when ≥2 products were actually promoted.

### Batch membership — what it is and when it closes

A **batch** is the set of sibling runs spawned from one signal (`run_batches`, keyed by
`batch_id`). The `run_batches.membership_closed` flag answers a single question: **"can more
runs still join this batch?"**

- `membership_closed = false` (**open**) — more runs may still be promoted in.
- `membership_closed = true` (**closed**) — the run set is final; no more will join.

The flag exists to **guard the Variant 2 synthesis trigger**. The cross-product memo must fire
exactly once, *after* every run in the batch has settled — but "all settled" is only meaningful
once the membership is final. If a promotion could still add a run a moment later, an early
synthesis would be computed over an incomplete set. So `batch_ready_for_synthesis` requires
`membership_closed == true` **and** all runs settled **and** > 1 run **and** no synthesis yet
(§6). The flag is the seal that says *"the set is final — now you may count."*

**Lifecycle under the conservative default:**

| Moment | Membership | Why |
|--------|-----------|-----|
| Fan-out, **deferred candidates exist** | left **open** | a promotion may still join — keep the seal off so synthesis waits |
| Fan-out, **no deferred candidate** (only the primary is relevant) | **closed immediately** | nothing can ever be promoted, so leaving it open would strand the batch *open forever* (no event ever closes it), accumulating dead single-run batches. Closing now also matches the legacy "no other relevant product → closed batch" behaviour. (A single-run batch never synthesizes anyway — the `> 1 run` guard — but the seal should still reflect reality.) |
| PM promotes a candidate | stays **open** | further promotions may still follow |
| PM declines further promotion (`POST /batch/{id}/close`) | **closed** | the set is now final; re-checks the synthesis trigger |

This inverts the default from **auto opt-out** (eager spray) to **human opt-in** (pull). It is
deliberately **reversible and engine-local**: `original_product_id`, `batch_id`, `run_batches`,
`portfolio_syntheses`, and the synthesis pass all stand unchanged. Decision-context, the
observatory schema, and the eval golden set are untouched.

**No migration for already-fanned batches.** Batches created under the eager default are all
`membership_closed = true` (the old code closed them on creation) and their runs are terminal, so
the new code treats them as inert: `promote` returns 409 on a closed batch (correct — they are
legacy), and retro-selecting a "primary" for already-settled runs would be meaningless. The only
cleanup is operational, not a schema migration: drain any run still parked at a gate via a normal
gate decision.

**Product families** (a `product_id → family` map in engine config; routing/grouping only — it
is *not* the workflow unit, and does not restructure per-product decision-context):

| Family | Products |
|--------|----------|
| Knox Enterprise Security | `example-security-product`, `example-mobile-product`, `example-governance-product`, `example-enterprise-ai-product` |
| Knox IAM | `example-identity-product` |
| Consumer GenAI | `example-consumer-product`, `example-agent-product` |

> **Promoting "product as the workflow unit → product-*family* as the unit"** was considered and
> deferred. It would merge per-product decision-context (which is genuinely differentiated —
> MTD's malware/phishing material vs AI-governance's pillar/threat-taxonomy framework), invalidate
> the per-product eval golden set, and bake today's product similarity into the architecture as a
> one-way door. Family stays a **routing layer** here; promotion to the workflow unit is a separate,
> later content project gated on living with this default first.

The sections below (§1–§8) document the original eager design and remain accurate for the 1:N
**plumbing**; read §5's "create one run per relevant product" as **"create the primary run; defer
the rest to promotion"** per this section.

---

## 1. Problem

Today a signal is permanently bound to exactly one product:

- `Signal.product_id` is a single `NOT NULL` column
  ([app/models/workflow.py](../app/models/workflow.py)).
- `POST /runs/start` rejects any run whose `product_id` does not match the
  signal's stored `product_id` with `422`
  ([app/api/runs.py](../app/api/runs.py) — the equality check is the hard gate).
- Every stage I/O schema carries a single `product_id`; `context_loader`
  loads exactly one product context per run.

A platform-level signal (OS / API / regulation change) that has implications for
several products cannot be represented. The only workaround is submitting the
same signal once per product, which duplicates `raw_content`, loses the link
between the resulting runs, and produces no portfolio-level view.

---

## 2. What we are (and are not) building

**In scope**
- **Option B** — 1:N plumbing: a signal can spawn runs across multiple products,
  linked as siblings (`batch_id`) for traceability.
- **Funnel (Option C as a cost filter)** — a cheap upfront Triage decides *which*
  products a signal is relevant to, so the expensive pipeline runs only for
  relevant products, never for all of them.
- **Variant 2 — post-hoc portfolio artifact** — once all sibling runs settle, a
  single synthesis pass produces a cross-product memo for *visibility*.

**Explicitly out of scope**
- Cross-product insight that **changes** an individual product's routing decision
  (that is Variant 1 — a blocking synthesis stage). Variant 2 is visibility only:
  each product keeps its own routing.
- S1 normalization sharing/caching across siblings (optimization; see §7).

---

## 3. Why a funnel, not naive fan-out

Running the full S1–S7 pipeline once per product on every incoming signal is
wasteful: most signals are relevant to zero or one product. The fix is a cheap
filter in front of the expensive fan-out.

```
Signal
  → S1 (normalize)          ← 1 call. product-agnostic, shared
  → Portfolio Triage        ← 1 call. signal vs N product "profiles"
       relevant = { p : score ≥ threshold }     (typically 0–2)
  → fan out to relevant products only:
       for each p: S2 (deep) → Gate 1 → S3 … S7
  → [Variant 2] when the batch settles → portfolio synthesis artifact
```

Cost per signal:

| | Full pipeline (S2–S7) runs | Fixed cost per signal |
|---|---|---|
| Naive fan-out | N (every product) | scales with product count |
| **Funnel (this design)** | **K (relevant only), K ≈ 0–2** | **2 LLM calls (S1 + Triage)** |

Portfolio Triage is a **single** LLM call that scores the signal against all
product *profiles* at once — not S2 run N times. It generalizes the existing
single-product auto-triage (`relevance < AUTO_TRIAGE_THRESHOLD → archive`,
[app/api/runs.py](../app/api/runs.py)) to the whole portfolio, and moves the
filter *ahead of* the expensive stages.

**Resolved:** the portfolio currently holds **7 real products** (the `samsung-*`
dirs; `_template` is a scaffold and excluded, `general` is special-cased). Single
digit → **Triage is a single LLM call, no prefilter** (§8.1). Each product profile
is a **compressed summary of `products/<name>/context.md`** (§8.2).

---

## 4. Data model changes ([app/models/workflow.py](../app/models/workflow.py))

### Signal
- Rename `product_id` → **`original_product_id`**, made **nullable**.
  - Semantics change from authoritative binding to **provenance/origin hint**.
    Authority over product routing now lives in Triage and in the `product_id`
    of the spawned runs.
  - `NULL` for product-agnostic intake (RSS / file_watch); Triage routes those
    entirely.

### WorkflowRun
- Add **`batch_id`** (`String`, nullable).
  - Groups the set of runs created from one fan-out. Chosen over grouping by
    `signal_id` because the same signal re-run for the same product over time
    also shares `signal_id`; `batch_id` isolates "this fan-out's sibling set"
    from historical re-runs, keeping the Variant 2 barrier precise.
  - `NULL` for legacy single runs (treated as a batch of one).

### New table: run_batches
A batch needs a lifecycle flag because membership is **dynamic** — the manual
Portfolio Scan gate (§5) can add runs *after* the first run has started, so the
synthesis trigger must not fire while more runs might still be added.

| column | notes |
|---|---|
| `batch_id` | primary key |
| `signal_id` | source signal |
| `membership_closed` | bool — `false` until the scan decision is resolved; the Variant 2 trigger is blocked while `false` (§6) |
| `created_at` | |

### New table: portfolio_syntheses (Variant 2)
| column | notes |
|---|---|
| `batch_id` | primary key — one synthesis per fan-out batch |
| `signal_id` | source signal |
| `content_md` | rendered portfolio memo |
| `content_json` | structured synthesis output (priority ranking, root cause, sequencing, conflicts, synergies) |
| `run_ids_json` | the sibling runs covered |
| `created_at` | |

Kept as dedicated tables (not `Artifact` rows) because they are batch/signal-scoped,
not run-scoped, and should not be forced onto a single run's `run_id` FK.

---

## 5. API changes

### POST /signals ([app/api/signals.py](../app/api/signals.py))
- `SignalCreate.product_id` → **`original_product_id`, optional**.

### POST /runs/start ([app/api/runs.py](../app/api/runs.py))
- Remove the `product_id == signal.product_id` **422 equality gate** (the current
  blocker of 1:N).
- Intake-dependent routing:
  - **Product-agnostic intake** (RSS / file_watch, `original_product_id` is
    `NULL`): run S1 once → Portfolio Triage → create one run per relevant
    product, sharing a `batch_id`. Close batch membership once the fan-out set is
    created.
    > **Superseded by §0:** spawn only the **primary** product and keep membership
    > **open**; the other relevant products become deferred candidates added via
    > promotion, not eager fan-out.
  - **Manual intake naming a product P**: create the run for P only. Triage is
    **not** run automatically. Whether to scan the rest of the portfolio is an
    **interactive human decision** (the Portfolio Scan gate, below) — not a
    request flag and not an always-on policy.
- Validate each `product_id` resolves to a context directory (422 if a configured
  product is missing).
- Each run independently runs S2 (deep) and then auto-triages / pauses for Gate 1
  based on **its own** product relevance — per-product conclusions differ.
- **Response shape (§8.4):** `{ batch_id, runs: [...] }`.

### Portfolio Scan gate (manual intake, §8.3 — human-in-the-loop)
When a manual submission named product P, the PM is asked *"this signal may also
affect other products — scan the portfolio?"* — and Triage runs **only if they say
yes**.

- **Timing: piggybacked on Gate 1** (§8.3). When the PM picks the processing depth
  for P at Gate 1, the same interaction offers a portfolio-scan toggle — one human
  touchpoint, two decisions (depth + scan).
- **Yes** → run Portfolio Triage (excluding P) → join newly relevant products into
  **P's existing `batch_id`** → then close batch membership.
- **No** → batch stays `{P}` → close batch membership.
- **Edge — P auto-triaged (no Gate 1):** still offer the scan as a standalone
  prompt; low relevance for P is itself a reason to check elsewhere. If P
  auto-triages, the batch stays open until this standalone scan decision resolves.

### Sibling & portfolio reads
- `GET /runs?batch_id=` (and/or `?signal_id=`) — list siblings. `RunResponse`
  gains `batch_id`.
- `GET /signals/{id}/portfolio` (or include synthesis under the batch query) — the
  Variant 2 memo.

---

## 6. Portfolio synthesis trigger (Variant 2)

Hooked into `finalize_run()` — the **single exit point** for all terminal
transitions ([app/services/run_finalizer.py](../app/services/run_finalizer.py)).

1. On any run reaching a settled state (`completed` / `killed` / `failed`), look
   up its `batch_id`.
2. Trigger synthesis only when **all** hold:
   - the batch's `membership_closed` is `true` (the scan decision is resolved — so
     no late runs can still be added), **and**
   - every run in the batch is settled, **and**
   - the batch has > 1 run, **and**
   - no synthesis row exists yet.
3. **Idempotency guard:** insert-or-skip on `portfolio_syntheses.batch_id` (PK), so
   two siblings finishing near-simultaneously cannot double-fire. Skip entirely for
   a single-run batch.

Synthesis logic (new `app/services/portfolio_synthesis.py`):
- Gather per-run key outputs across the batch (S2 relevance/insight, S5 routing +
  composite, S7 summary), deduped by product.
- One LLM call via `LLMProvider` (Development Rule 4 — no SDK imports in services)
  producing: portfolio priority ranking, shared root cause, sequencing/dependencies,
  resource conflicts, synergies. Each product keeps its own routing — visibility,
  not override.
- Persist to `portfolio_syntheses`.
- Emit a `portfolio_synthesized` event. **pm-engine writes no files** — Hermes
  consumes the event if it wants to sync to the wiki (Development Rule 8 /
  [EXPORT_AND_SYNC_CONTRACT.md](./EXPORT_AND_SYNC_CONTRACT.md)).

### Worked example
Signal: *"Android 16 adds mandatory restrictions on device-owner background
behaviour; some MDM background-monitoring APIs are deprecated."* Triage fans out
to three products, each producing its own siloed result:

| product | relevance | routing | composite |
|---|---|---|---|
| knox-mtd | 4.6 | prd | 4.2 |
| knox-lockdown-mode | 3.8 | poc | 3.6 |
| knox-ai-governance | 2.1 | kill | — |

The synthesis memo then adds what no single run could see: both knox-mtd and
knox-lockdown-mode hit the **same** deprecated API → replace one shared monitoring
layer rather than two; sequence knox-mtd's PRD first with lockdown-mode as a
dependent follow-up; warn that both compete for the same Q3 platform capacity.
knox-ai-governance is unaffected. Routings are unchanged; the memo is a
portfolio-level reading on top of them.

---

## 7. Scope boundaries, risk, and follow-ups

- **Existing eval stays green.** A single-product signal produces one run = a batch
  of one → synthesis is skipped. `eval/scenarios.json`'s one-signal / one-routing
  assumption is unaffected. A multi-product scenario is an optional follow-up.
- **S1 sharing is deferred.** v1 re-runs S1 per run (current behaviour). Caching the
  product-agnostic normalization onto the signal (consistency + cost) is a follow-up.
- **Gate notifications.** K relevant products → K Gate 1 / Gate 2 notifications.
  Synthesis is post-hoc and adds no gate noise; a single "portfolio ready"
  notification is optional.
- **Schema migration.** `original_product_id` rename, `batch_id` column, and the
  `run_batches` + `portfolio_syntheses` tables need a migration path; confirm how
  the current schema is created/migrated before landing.
- **Triage scaling.** A single LLM call over 7 profiles is comfortable. Revisit
  prefiltering only if the portfolio grows to dozens.

---

## 8. Resolved decisions

1. **Triage mechanism** → **LLM-only, single call** (7 real products — single digit;
   no embedding/keyword prefilter).
2. **Product profile source** → **compressed summary of `products/<name>/context.md`**
   (no separate `relevance-profile.md`).
3. **Manual-submission Triage** → **interactive Portfolio Scan gate, piggybacked on
   Gate 1** (human-in-the-loop). Triage runs only on an explicit "yes"; never
   always-on. Introduces dynamic batch membership → `run_batches.membership_closed`
   gates the synthesis trigger (§4, §6).
4. **`/runs/start` response shape** → **`{ batch_id, runs: [...] }`**.
5. **Portfolio prompt location** → **new product-agnostic `portfolio/` area in
   `decision-context-companion-repo`**, loaded via `template_service`. This **extends**
   the CLAUDE.md principle from "workflow design is product-scoped and owned by
   decision-context" to "product- *and portfolio*-scoped"; the synthesis prompt
   belongs to no single product folder, so a cross-product area is added rather than
   moving the prompt into engine code. CLAUDE.md to be updated when this lands.
```
