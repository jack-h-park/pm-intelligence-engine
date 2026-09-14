# Insight Product Connection Design

## Status

Approved for implementation on 2026-09-13. This design adds a read-only, evidence-grounded product-connection surface for an existing Insight. It does not authorize S2K intake, automatic product mapping, automatic DecisionCase creation, or decision-pipeline activation.

## Problem

An Insight can be useful without belonging to a product. The existing legacy Portfolio Triage is not an acceptable solution: it scores a raw signal with an LLM and automatically starts the highest-ranked product run. Reusing it would turn a non-binding recommendation into an implicit decision and would mix the legacy signal workflow with S2K.

The current S2K fixture replay proves one bounded happy path. It does not prove duplicate handling, weak-evidence rejection, usefulness across different signal shapes, or quality of a product connection. One observed production Insight is therefore insufficient for an autonomous S2K-to-decision cutover.

## Invariants

1. An Insight remains product-agnostic. No `product_id`, selected candidate, or ranking is persisted on the Insight, its revision, bundle, or prepared context.
2. A connection response is computed only from an immutable Insight revision, its cited passages, and versioned product profiles.
3. A connection must cite one or more Insight passage IDs. A product profile alone cannot justify a connection.
4. `no_clear_connection` is a successful result, not an error or a fallback to a generic product.
5. The response never creates a Signal, run, batch, DecisionCase, delivery receipt, or feedback record.
6. The Console must not preselect a product. A reviewer may explicitly copy a candidate into the decision form, but must still submit a product and decision question to create a decision request.
7. `DECISION_PIPELINE_V2_ENABLED` remains the only gate for the existing write endpoint. This read endpoint must be available independently and must not change that flag.
8. S2K intake and all legacy scanner/auto-submit jobs remain paused during implementation and calibration.

## Boundary

```
immutable Insight + cited passages + versioned product profiles
                         |
                         v
          read-only ProductConnection assessment
                         |
              0..N candidates / no clear connection
                         |
                         v
           reviewer explicitly selects product + writes question
                         |
                         v
    existing, separately feature-gated Decision Request endpoint
```

The assessment is not a mapping. It is neither a product field nor a workflow transition.

## API contract

`GET /insights/{insight_id}/product-connections?revision=<n>` returns an ephemeral response:

```json
{
  "insight_id": "...",
  "revision": 1,
  "assessment": "candidates",
  "profile_revision": "<content hash>",
  "candidates": [
    {
      "product_id": "android-enterprise",
      "product_title": "Android Enterprise",
      "confidence": "medium",
      "rationale": "The cited work-profile passage concerns the product's managed-profile boundary.",
      "passage_ids": ["source-1:0"]
    }
  ],
  "alternatives": [
    {
      "product_id": "samsung-knox-mtd",
      "reason": "Related mobile security concern, but the Insight does not cite a threat-detection mechanism."
    }
  ]
}
```

For no supported connection, `assessment` is `no_clear_connection`, `candidates` is empty, and the response states why the evidence did not support a target. The endpoint returns 404 for a missing Insight and 409 if an explicitly requested revision is not current. It does not write to either store.

## Evaluation strategy

### Fixture corpus

Replace the current fixture-only metadata with an executable corpus. The corpus must include at least these twelve semantic cases:

1. actionable personal learning with no product connection;
2. one strongly supported Android Enterprise connection;
3. one strongly supported security-product connection;
4. two plausible products with no primary recommendation;
5. superficially matching but unsupported product terms;
6. weak or fallback-only evidence;
7. duplicate content;
8. stale historical context;
9. late body / title-only signal;
10. superseded evidence;
11. repeated signal with no material delta;
12. malformed or missing provenance.

Each case declares expected S2K disposition, expected connection assessment, candidate IDs when applicable, and required cited passage IDs. Fixtures use a local deterministic LLM/provider; they make no OAuth, Tavily, Firecrawl, or paid fetch calls.

### Acceptance criteria

* All 12 cases replay without a legacy Signal, legacy workflow run, DecisionCase, or delivery receipt.
* Every returned candidate cites an Insight passage and has a rationale that refers only to the profile plus cited evidence.
* `no_clear_connection` is returned for every weak, stale, duplicate, malformed, or unsupported case.
* Ambiguous cases return alternatives without an implicit primary selection.
* The existing Decision Request test continues to prove that only reviewer-provided `product_id` and `confirmed_product_id` can create a request.
* The Console contract test proves no select default and no POST merely from rendering or loading recommendations.

## Recommendation implementation stages

### Stage 1: calibrated, transparent candidate generation

The Engine loads compressed versioned profiles using the existing context-loader boundary, but uses a dedicated `ProductConnectionService`, not `portfolio_triage`. The first profile contract adds an optional `## Connection Anchors` section to a product context. Each non-empty bullet is a reviewed phrase that may be matched only against an Insight claim's cited passage. A candidate requires at least one exact normalized anchor match; a tie produces alternatives, never an implicit primary. Products without anchors remain eligible for a later semantic assessor but produce no Stage 1 candidate. This gives the corpus a deterministic, explainable evaluator without provider calls. In production, it returns `no_clear_connection` until a profile/evidence match is defensible.

### Stage 2: semantic ranking only after calibration review

If the fixture corpus shows that transparent matching misses valid semantic relationships, add a separately injected OAuth-backed semantic assessor. It must use a zero-default allowance, write no mapping, return the same response shape, and be measured against the frozen corpus before activation. It must not reuse the legacy raw-signal triage prompt or auto-start semantics.

## Rollout and rollback

This release is read-only. Deploying it does not resume scanners or enable the decision pipeline. Rollback is application rollback; there is no migration or durable connection data to clean up. The next release decision is based on fixture results and manual review of several intentionally created, varied Insights—not elapsed observation time alone.
