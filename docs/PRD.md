# Product Requirements Document
## pm-intelligence-engine

**Version:** 1.0  
**Status:** Draft  
**Last updated:** 2026-05-19

---

## 1. Problem Statement

### The Core Problem

A PM's time should be spent on judgment — deciding what matters, what to build, and why. In practice, the majority of time goes to *adjacent work*: gathering signals, normalizing information, formatting documents, filing records.

The existing `decision-context-companion-repo` has a well-designed 7-stage decision workflow, but every stage requires manually writing markdown files. This friction interrupts focus and slows the cadence of decision-making.

### Specific Pain Points

| Pain Point | Impact |
|---|---|
| Signal collection is ad hoc | High-signal events get missed; no systematic coverage |
| Each stage requires a new file to be written manually | Context-switching overhead; workflow stalls mid-run |
| Decision rationale is scattered across files | Hard to reference past decisions when a similar signal appears |
| Signal → decision → wiki sync is fully manual | Learning doesn't accumulate automatically |

### Target State

> The PM sees a signal, reviews the analysis, approves at the gate, and receives a complete PRD or Executive Summary — without writing a single file.

---

## 2. Users

**Primary user:** Single PM (personal use)

### Usage Scenarios

| Scenario | Trigger | Expected Outcome |
|---|---|---|
| **Signal discovery** | Competitor announcement, platform update, regulatory change | Signal is logged and workflow starts automatically |
| **Weekly market review** | Scheduled (Monday morning) | A list of auto-collected signals is ready for triage |
| **Judgment review** | S4 evaluation complete | PM reviews 4 persona evaluations, approves or requests revision |
| **Artifact use** | S7 complete | PRD or Executive Summary is immediately ready to use or share |
| **Historical reference** | Similar signal reappears | PM queries past runs to see how comparable signals were decided |

---

## 3. Features

### F1 — Manual Signal Input

**Purpose:** Log a signal discovered while browsing, reading, or in a meeting — without workflow interruption.

**User flow:**
1. POST a URL or raw text with a product selection
2. Signal is stored and ready to start a workflow run

**API:** `POST /signals { url?, raw_text, product_id }`

**Acceptance criteria:**
- Signal saved to DB with category, source type, and status = `pending`
- Queryable in signal list immediately

---

### F2 — Automated Signal Collection

**Purpose:** Ensure relevant market signals are captured even when the PM isn't actively monitoring.

> **Ownership update (2026-05):** Automated harvesting is no longer an in-process
> pm-engine feature. Hermes owns scheduled harvesting and submits signals via
> `POST /signals`. This section remains as a product capability requirement, not
> an implementation requirement for this repository.

**Behavior:**
- Hermes polls configured RSS feeds and URLs on a schedule
- Hermes watches `product-management-wiki-repo/raw/from-web/sensing/` for new files
- Hermes references `products/<name>/signal-sources.md` for per-product source lists
- pm-engine deduplicates only through its explicit ingestion path and storage rules

**Acceptance criteria:**
- New signals added to DB via the public API with `source_type = rss` or `file_watch`
- Duplicate signals (same URL) are not re-inserted

---

### F3 — Signal List Query

**Purpose:** Browse collected signals to decide which to analyze next.

**API:** `GET /signals?product_id=&status=&limit=`

**Acceptance criteria:**
- Returns signals filtered by product, status (`pending` / `in_run` / `done`), and date
- Each signal shows: title, source URL, category, status, ingested_at

---

### F4 — Workflow Run Start

**Purpose:** Turn a signal into a full analysis run. S1 through S4 execute automatically.

**User flow:**
1. `POST /runs/start { signal_id, product_id }`
2. System loads 3-layer context (PM Identity + Company Context + Product Context)
3. S1 → S2 → S3 → S4 execute sequentially
4. Run status moves to `waiting_approval` when S4 completes

**Acceptance criteria:**
- Each stage output is stored as structured JSON in `StageOutput`
- Run status transitions: `pending` → `running` → `waiting_approval`
- Context loading reads from `decision-context-companion-repo/core/` and `products/<name>/context.md`

---

### F5 — S4 Persona Evaluation (Multi-agent)

**Purpose:** Reduce single-viewpoint bias by running four independent evaluations in parallel.

**Agents and their roles:**

| Agent | Core Question | Guards Against |
|---|---|---|
| Explorer | "How far can we go with this?" | Thinking too small |
| Strategist | "Does this belong in our direction?" | Opportunistic drift from strategy |
| Builder | "Can we actually make this?" | Ideation without grounding |
| Skeptic | "What if we're wrong about this?" | Confirmation bias |

**Output per agent:** `score` (1–5), `key_argument`, `open_question`

**Quality gate:** S4 rubric auto-score (12-point checklist). If Skeptic agrees with all other agents, flag as potential confirmation bias.

**Acceptance criteria:**
- 4 persona outputs stored independently (no blending)
- S4 rubric score computed and stored alongside outputs

---

### F6 — PM Approval Gate (Human-in-the-Loop)

**Purpose:** Ensure PM judgment cannot be bypassed. The system pauses and waits for an explicit decision before proceeding to scoring and routing.

**API:**
- `GET /runs/{id}` — view full S4 output before deciding
- `POST /runs/{id}/approve` — proceed to S5
- `POST /runs/{id}/revise { feedback }` — re-run S4 with feedback as additional context
- `POST /runs/{id}/reject { reason }` — terminate run

**Acceptance criteria:**
- Approval event stored with `action`, `feedback_text`, `created_at`
- `approve` triggers S5 execution immediately
- `revise` re-runs S4 with feedback injected into the prompt context
- `reject` moves run to `killed` status with reason stored

---

### F7 — S5 Scoring and Routing

**Purpose:** Convert qualitative persona evaluations into a numeric decision and determine the next track.

**Scoring formula:**
```
Composite = (Impact × 0.35) + (Strategic Fit × 0.30) + (Feasibility × 0.20) + (Confidence × 0.15)
```

**Routing rules:**
- `Composite ≤ 2.0` → **Kill** (run terminates, reason recorded)
- `Confidence 2–3` → **S6A PoC Plan**
- `Confidence 4–5` → **S6B PRD Draft**

**Assumption classification:** Each assumption in the opportunity is classified as:
- **Blocking** — if false, the entire opportunity is invalidated
- **Informing** — if false, scope narrows but opportunity survives

**Acceptance criteria:**
- `composite_score`, `routing`, and `assumption_list` saved to `WorkflowRun`
- Kill routing terminates the run and records the kill rationale

---

### F8 — S6A PoC Plan / S6B PRD Generation

**Purpose:** Produce the right execution artifact based on confidence level.

**S6A PoC Plan output:**
- Assumptions being tested (prioritized from Blocking list)
- Minimum experiment design (scope, duration, resource estimate)
- Success and failure criteria (measurable)

**S6B PRD output:**
- Goal and success metric
- 3–5 user stories
- In-scope / out-of-scope lists
- Measurement method for each success metric
- Open questions

**Quality gate (S6B):** 12-point PRD completeness checklist — an engineer unfamiliar with the run should be able to start implementation from the document alone.

**Acceptance criteria:**
- Artifact saved as both `content_md` (Markdown) and `content_json` (structured)
- S6B: PRD checklist score computed and stored

---

### F9 — S7 Executive Summary

**Purpose:** Compress the full run into a one-page decision brief for stakeholders.

**Output structure:**
1. **What we saw** — signal summary
2. **What it means** — strategic implication (pillar reference)
3. **What we decided** — routing + composite score + rationale
4. **What we will do next** — concrete action (with owner and timeline if applicable)

**Acceptance criteria:**
- Every recommendation includes source links and references to stage outputs
- Saved as Markdown + JSON
- Summary does not introduce new claims not present in S1–S6 outputs

---

### F10 — Run File Export

**Purpose:** Write completed run artifacts to the existing `decision-context-companion-repo` folder structure so the new system and manual workflow remain compatible.

**Behavior:**
- Creates `products/<name>/runs/<YYYY-MM-DD>-<slug>/`
- Writes `s1-signal.md` through `s7-report.md` in the same format as manual runs
- Does not overwrite existing files

**Acceptance criteria:**
- Exported files are structurally identical to manually-created run files
- Existing run files are not modified

---

### F11 — Run History Query

**Purpose:** Find past decisions to inform current judgment.

**API:**
- `GET /runs?product_id=&status=&routing=&limit=`
- `GET /runs/{id}` — full run with all stage outputs

**Acceptance criteria:**
- Filterable by product, routing decision (PRD / PoC / Kill), status, date range
- Returns composite score, routing, current stage, and stage output summaries

---

### F12 — Wiki Auto-Sync

**Purpose:** Automatically ingest completed S7 reports into the wiki so pattern accumulation happens without manual steps.

**Behavior:**
- On S7 completion, copies the report to:
  - `raw/from-pm-decision-context/kills/` for Kill decisions
  - `raw/from-pm-decision-context/prds/` for PRD decisions
  - `raw/from-pm-decision-context/poc-upgrades/` for PoC decisions
- Generates YAML frontmatter: `source`, `type`, `date`, `run`, `review_needed: false`

**Acceptance criteria:**
- File created in the correct wiki subdirectory with valid frontmatter
- Source file in `pm-decision-system` is not modified

---

### F13 — Eval Harness

**Purpose:** Measure output quality continuously so prompt changes or model upgrades don't silently degrade judgment quality.

**Golden dataset:** Historical runs R01–R07 from `example-security-product`

**Evaluation dimensions:**

| Metric | Method | Expected Result |
|---|---|---|
| S5 routing accuracy | Re-run S1–S5 on R01–R07 signals | R06 → Kill, R04/R05/R07 → PRD |
| S4 rubric score | Auto-evaluate against 12-point checklist | Score ≥ historical baseline |
| S3 falsifiability check | Verify hypothesis is testable and bounded | Pass for all historical runs |

**Acceptance criteria:**
- `python eval/runner.py` runs all scenarios and outputs a pass/fail report
- New features cannot be considered complete if routing accuracy drops below baseline

---

## 4. Non-Goals (v1)

- No web UI — API-only
- No multi-user support
- No real-time Slack notifications (v4+)
- No semantic search over wiki content (v4+)
- No LangGraph or Prefect (v3+)
- No PostgreSQL (v2+)

---

## 5. Success Metrics

| Metric | Target |
|---|---|
| End-to-end run time (S1–S7) | < 3 minutes per run |
| S5 routing accuracy on golden dataset | 100% match to historical decisions |
| PM time saved per decision cycle | > 60 minutes vs. manual workflow |
| Eval harness passes after each phase | All scenarios green before moving to next phase |
