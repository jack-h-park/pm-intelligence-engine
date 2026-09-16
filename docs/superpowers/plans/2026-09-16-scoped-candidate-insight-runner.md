# Scoped Candidate Insight Runner

## Goal

Advance one explicitly named, already-persisted S2K Candidate into an evidence bundle and an OAuth-analyzed Insight without claiming the generic learning queue, fetching new sources, creating a Product Decision request, or delivering a notification.

The first controlled execution target is production Candidate `2e22eb60-761e-4c26-84bc-34d9ec513f93` (Calif OEMpocalypse). It already has one immutable public source. This document does not authorize its execution; it defines the safe code path that makes the later approved execution auditable.

## Non-goals

- Do not replay legacy Gate 0 records or change its retained read-only history.
- Do not call discovery, Tavily, Firecrawl, or any new source fetcher.
- Do not assign, suggest, or confirm a product.
- Do not create a DecisionCase, decision request, delivery receipt, Telegram message, or scheduler work.
- Do not let a scoped tick fall through to `claim_job()`.

## Design

1. Add a persisted `scoped_candidate_runner` marker to `InsightJob`. It is opt-in and defaults to `false`, so existing jobs preserve their behavior.
2. Add an atomic store operation that accepts a Candidate ID, loads only its stored source rows, prepares an attributable bundle from those rows, and creates at most one marked job. Repeated calls return the same job and bundle.
3. Add a dedicated lease method that selects only the marked job for that Candidate. It may recover only that job's expired lease; it never iterates, releases, or claims another job.
4. Add an OAuth worker entrypoint and authenticated API endpoint that call the scoped methods. The response exposes the target Candidate, job state, completion disposition, and resulting Insight ID when available.
5. Add a Hermes command wrapper that requires a UUID, invokes only the dedicated endpoint, and prints the returned reviewable IDs. It must not scan, fetch, or use the generic worker route.

## Test-first implementation tasks

### Task 1: Engine store and worker isolation

Add store/worker tests before code:

- A scoped Candidate with one stored source produces a bundle and a marked job; re-invocation is idempotent.
- The scoped lease leaves an unrelated queued job untouched.
- A scoped Candidate with no usable stored source reaches the explicit `needs_evidence` outcome without any new acquisition.
- An expired lease recovery is limited to the named Candidate's marked job.

Implement only the minimal model/store/worker support to satisfy those tests.

### Task 2: Engine endpoint and contract

Add API tests before code:

- `POST /insight-candidates/{candidate_id}/worker-tick` invokes the scoped OAuth entrypoint and returns the target status.
- A missing Candidate is a 404.
- The endpoint cannot use the generic queue; an unrelated queued job remains queued.

Implement the endpoint with the existing authentication dependency pattern and no background task/scheduler registration.

### Task 3: Hermes explicit runner

Add a command/test fixture for a runner requiring `--candidate-id` (UUID) and calling only the scoped endpoint. Preserve the current API base/token handling, with no source acquisition or generic runner fallback.

### Task 4: Verification, deployment, and controlled run

1. Run focused Engine tests, full insight/decision tests, lint, docs checks, and CI.
2. Merge and deploy Engine first; verify the production health endpoint and deployed revision.
3. Merge and deploy Hermes runner; verify its `--help` and its exact production target configuration without exposing credentials.
4. Immediately before the Calif run, verify no unrelated queued job can be affected.
5. Run exactly once against Candidate `2e22eb60-761e-4c26-84bc-34d9ec513f93`.
6. Verify resulting source/bundle/job/Insight lineage, absence of Product Decision and delivery records, and render the canonical console detail route for human review.

## Acceptance criteria

- Every mutation is attributable to the requested Candidate ID.
- No generic job state changes in the isolation test or production pre/post check.
- The analysis is OAuth-only, bounded to the already stored evidence, and reports a reviewable terminal state.
- An Insight, if created, remains product-agnostic and is not automatically delivered or promoted.
