# Insight Review Checkpoint

## Status

Implemented and verified locally on 2026-09-14. Console review UI remains a
separate rollout slice.

## Purpose

An Insight is a personal-learning record, not a Product Decision input. A
reviewer needs a durable way to acknowledge that they assessed an Insight and
to record the outcome before the bounded signal-review pilot can count it as
reviewed.

## Contract

- Every review is tied to one immutable `insight_id` and `revision`.
- The permitted dispositions are `retain`, `needs_evidence`, and
  `not_useful`. They describe the learning record only.
- A review may include a bounded optional note and retains a hashed actor
  identifier, never a bearer token or a product identifier.
- Submitting a review never assigns a product, creates a `DecisionCase`,
  starts a legacy run, or changes the Insight revision.
- Creating a review requires bearer authentication and an `Idempotency-Key`.
  Repeating the same request replays the original review; reusing that key
  with different content conflicts.
- Reading an Insight and listing its reviews are side-effect free.

## Product Decision Boundary

The existing `POST /insights/{insight_id}/decision-requests` endpoint remains
the only bridge from an Insight to Product Decision. It requires an explicit
reviewer-confirmed product ID and question. The review endpoint intentionally
does not accept those fields.

## Rollout

1. Add the Engine persistence and API contract with focused tests.
2. Add a Console read/detail surface that calls the authenticated Engine API
   server-side and lets a reviewer submit one explicit disposition.
3. Review the already-created Gigabud Insight plus two or three bounded new
   Insights. Do not reactivate discovery until that review evidence exists.
