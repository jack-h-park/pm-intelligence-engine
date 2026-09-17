# Incremental Insight Sync Contract

## Purpose

`GET /insights` is the Engine-owned read interface for consumers that need Insights created after a known point. Consumers must not maintain a parallel catalogue of Insight IDs or reinterpret source material.

## Fixed evidence response gate

`GET /insights/{insight_id}/evidence?revision=<n>` is a revision-pinned public read contract. Its complete response object remains:

```json
{
  "insight_id": "uuid",
  "revision": 1,
  "sources": [],
  "passages": [],
  "claim_passage_links": [],
  "coarse_evidence": false
}
```

The endpoint returns cited source metadata and cited passages only. An exact-object test must continue to prove that uncited passages are absent.

## Incremental list contract

`GET /insights?since=<UTC ISO-8601>&limit=<1..100>&after=<opaque cursor>` returns immutable Insights created strictly after `since`, in ascending `(created_at, insight_id)` order.

- `since` is optional; omitting it retains the existing full-list behavior.
- A correction is a newly created immutable Insight linked by `supersedes_insight_id`, so it has a new `created_at` and is returned as new work. This slice does not invent mutable revision timestamps.
- `after` is Engine-issued base64url JSON containing the original `since` boundary and final `(created_at, insight_id)` tuple. It cannot be reused with a different `since` boundary.
- `next_cursor` is null only when no matching row remains. Consumers retain this Engine token, not their own Insight-ID ledger.
- Malformed datetimes/cursors and mismatched cursor boundaries return 422. Tokens contain no source body, credentials, or workflow state.

The list response remains `{"items": [...], "next_cursor": "..." | null}`. Search remains unpaginated in this slice.

## Acceptance criteria

1. The evidence endpoint exact-object fixture pins all six fields and omits uncited passages.
2. `since` excludes an older Insight and includes a later replacement linked by `supersedes_insight_id`.
3. Two identical timestamps paginate once each by `insight_id`.
4. Invalid cursors and cursor/since mismatches return 422.
5. Existing unauthenticated, limit-only, detail, and search behavior remains covered.

## Non-goals

No Hermes scheduling, wiki projection, source acquisition, Product Decision, product mapping, delivery, or schema migration is introduced.
