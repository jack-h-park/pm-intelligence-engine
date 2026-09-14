# Insight Product Connections Implementation Plan

> Execute in an isolated worktree. Keep documentation and code comments in English.

## Scope

Implement the Stage 1 read-only product-connection contract and fixture calibration. Do not modify production flags, scanner schedules, legacy Portfolio Triage, Insight persistence schema, or the Decision Request write path.

## 1. Establish failing contract tests

Add focused API/service tests that create an Insight, immutable prepared context, cited evidence bundle, and temporary decision-context product profiles. Cover:

* a cited, supported single candidate;
* no clear connection;
* ambiguous alternatives without a selected primary;
* missing Insight and stale revision;
* no writes to either workflow store;
* no invocation of the legacy `portfolio_triage` service.

## 2. Add read-only domain models and service

Create response models with `assessment`, revision identity, profile revision, candidates, alternatives, rationale, confidence, and cited passage IDs. Add a service that:

1. loads the requested immutable Insight revision and prepared context;
2. validates that candidate passage IDs belong to the evidence bundle;
3. loads compressed portfolio profiles and their optional reviewed `## Connection Anchors` through `ContextLoader`;
4. delegates candidate assessment to an injected, non-persisting evaluator;
5. rejects invalid evaluator output rather than inventing a mapping.

The first evaluator will be fixture-deterministic. Production activation of an LLM evaluator is explicitly deferred.

## 3. Expose the Engine endpoint

Add `GET /insights/{insight_id}/product-connections`. It is read-only, has no write feature flag, and must not call the decision-request endpoint. Register it before the catch-all Insight detail route.

## 4. Expand the fixture replay harness

Turn `cases.json` into a twelve-case executable corpus. Extend the replay report with S2K disposition, connection assessment, candidate IDs, cited passages, and store-side-effect counters. Keep the one-command replay local and provider-free.

## 5. Console read-only rendering

In a separate Console worktree/PR, request and render the connection response beneath Insight evidence. Show rationale, cited passages, alternatives, and the explicit no-connection state. Do not default the product selector. A candidate action may copy an ID only after a deliberate click; it must not submit the form.

## 6. Verify

Run the targeted Engine tests, fixture replay, formatting/type checks, and the full relevant suite. Review `git diff` to confirm no store migration, no write route behavior, and no changes to `portfolio_triage`. Do not deploy until the Engine and Console changes have passed review.

## 7. Release gate

After both read-only PRs are merged and deployed, retain:

```text
DECISION_PIPELINE_V2_ENABLED=false
INTELLIGENCE_SENSING_ALLOWANCE_MICROS=0
INTELLIGENCE_DECISION_ALLOWANCE_MICROS=0
```

Keep scanner jobs paused. Review the fixture matrix and a deliberately varied manual sample before considering a semantic-ranking pilot or decision-pipeline activation.
