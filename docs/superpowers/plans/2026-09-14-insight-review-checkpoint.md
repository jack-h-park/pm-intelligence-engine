# Insight Review Checkpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a durable, product-agnostic human review checkpoint for immutable Signal-to-Knowledge Insights.

**Architecture:** The Engine persists append-only review records keyed to an Insight revision and exposes authenticated, idempotent create and read endpoints. The Console will consume that API server-side in a separate task; neither layer can use a review to assign a product or create a Product Decision.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy, SQLite, pytest.

**Spec:** `docs/design/insight-review-checkpoint.md`

## Global Constraints

- Review records are product-agnostic and must not contain `product_id`.
- A review must not create a `DecisionCase`, a legacy workflow run, or change an Insight revision.
- Review writes require bearer authentication and an `Idempotency-Key`.
- Persist only an actor fingerprint, never the bearer credential.
- Existing feedback labels remain independent of the review disposition.

---

### Task 1: Define the immutable review record and persistence

**Files:**
- Modify: `app/models/insights.py`
- Modify: `app/storage/insight_store.py`
- Test: `tests/insights/test_insight_store.py`

**Interfaces:**
- Produces: `InsightReview`, an immutable Pydantic record containing `review_id`, `insight_id`, `revision`, `disposition`, `note`, `actor_fingerprint`, and `created_at`.
- Produces: `InsightStore.save_idempotent_review(actor, key, request_hash, payload) -> tuple[InsightReview, int]` and `InsightStore.list_reviews(insight_id) -> list[InsightReview]`.

- [x] **Step 1: Write the failing storage tests**

```python
review, status_code = store.save_idempotent_review(actor, "review-1", request_hash, payload)
assert status_code == 201
assert store.list_reviews(insight.insight_id) == [review]
```

- [x] **Step 2: Run the focused storage test to verify it fails**

Run: `python -m pytest tests/insights/test_insight_store.py -q`

Expected: FAIL because `InsightStore.save_idempotent_review` is absent.

- [x] **Step 3: Add the SQL row, public record, and store methods**

```python
class InsightReview(_Record):
    disposition: Literal["retain", "needs_evidence", "not_useful"]
    note: str | None = Field(default=None, max_length=2000)
```

Use the existing generic `create_idempotent` method with operation name `insight_review`, and query records in ascending `created_at` order.

- [x] **Step 4: Run the focused storage test to verify it passes**

Run: `python -m pytest tests/insights/test_insight_store.py -q`

Expected: PASS.

- [x] **Step 5: Commit the persistence slice**

```bash
git add app/models/insights.py app/storage/insight_store.py tests/insights/test_insight_store.py
git commit -m "feat: persist insight review records"
```

### Task 2: Expose authenticated review endpoints without a decision bridge

**Files:**
- Modify: `app/api/insights.py`
- Test: `tests/insights/test_api.py`

**Interfaces:**
- Consumes: `InsightStore.save_idempotent_review` and `InsightStore.list_reviews`.
- Produces: `POST /insights/{insight_id}/reviews` and `GET /insights/{insight_id}/reviews`.

- [x] **Step 1: Write the failing API test**

```python
response = client.post(
    f"/insights/{insight.insight_id}/reviews",
    json={"revision": insight.revision, "disposition": "retain", "note": "Useful boundary."},
    headers={**auth_headers, "Idempotency-Key": "review-api-1"},
)
assert response.status_code == 201
assert "product_id" not in response.json()
```

- [x] **Step 2: Run the focused API test to verify it fails**

Run: `python -m pytest tests/insights/test_api.py -q`

Expected: FAIL with a 404 response for the review endpoint.

- [x] **Step 3: Add request validation and endpoint handlers**

```python
@router.post("/insights/{insight_id}/reviews", status_code=status.HTTP_201_CREATED)
async def create_insight_review(...):
    review, stored_status = _store(engine).save_idempotent_review(...)
    response.status_code = stored_status
    return review
```

Validate that the Insight exists and the supplied revision matches its current immutable revision before saving. Pass `_actor_fingerprint(authorization)` to storage. Do not import or call the decision service.

- [x] **Step 4: Run the focused API test to verify it passes**

Run: `python -m pytest tests/insights/test_api.py -q`

Expected: PASS.

- [x] **Step 5: Commit the API slice**

```bash
git add app/api/insights.py tests/insights/test_api.py
git commit -m "feat: add insight review checkpoint API"
```

### Task 3: Verify the safety boundary and document the rollout

**Files:**
- Modify: `docs/design/insight-review-checkpoint.md`
- Modify: `docs/superpowers/plans/2026-09-14-insight-review-checkpoint.md`
- Test: `tests/insights/test_api.py`

**Interfaces:**
- Consumes: the review endpoints from Task 2.
- Produces: an executable, tested contract that review does not promote an Insight to Product Decision.

- [x] **Step 1: Add a regression test for decision isolation**

```python
assert engine.store is None
assert client.post(review_url, json=payload, headers=headers).status_code == 201
assert client.get(f"/insights/{insight.insight_id}/reviews", headers=auth_headers).status_code == 200
```

- [x] **Step 2: Run the focused suite**

Run: `python -m pytest tests/insights -q`

Expected: PASS.

- [x] **Step 3: Update the design status and plan checkboxes with the verified result**

Set the design status to `Implemented and verified locally` only after the focused suite passes. Mark only completed plan steps.

- [x] **Step 4: Run the relevant broader suite**

Run: `python -m pytest tests/insights tests/decision -q`

Expected: PASS.

- [x] **Step 5: Commit the contract and verification evidence**

```bash
git add docs/design/insight-review-checkpoint.md docs/superpowers/plans/2026-09-14-insight-review-checkpoint.md tests/insights/test_api.py
git commit -m "docs: record insight review checkpoint contract"
```
