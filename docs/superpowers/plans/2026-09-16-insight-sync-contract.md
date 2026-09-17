# Incremental Insight Sync Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose deterministic, paginated `GET /insights?since=` retrieval while preserving the fixed cited-evidence response contract.

**Architecture:** The Engine owns all change semantics. `since` filters immutable `IntelligenceInsightRow.created_at`; corrections are new Insights linked by `supersedes_insight_id`. An opaque cursor carries the original boundary and final `(created_at, insight_id)` tuple so no equal-timestamp result is missed and no consumer needs an Insight-ID ledger.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy, SQLite, pytest.

**Spec:** `docs/design/2026-09-16-insight-sync-contract.md`

## Global Constraints

- Keep `GET /insights/{insight_id}/evidence?revision=<n>` fields exactly `insight_id`, `revision`, `sources`, `passages`, `claim_passage_links`, and `coarse_evidence`.
- Preserve immutable Insight records; replacements remain new records.
- Add no Hermes scheduler, source acquisition, product mapping, Product Decision, delivery, or database migration.
- Keep `GET /insights` authenticated and `limit` constrained to 1 through 100.
- Use UTC ISO-8601 datetimes; malformed `since`/`after` values return 422.

---

### Task 1: Name and retain the fixed evidence-response gate

**Files:**
- Modify: `tests/insights/test_api.py:243-300`
- Modify: `docs/design/2026-09-16-insight-sync-contract.md`

**Interfaces:**
- Consumes: `GET /insights/{insight_id}/evidence?revision=<n>` and `InsightEvidenceView`.
- Produces: A named exact-object regression test for the public evidence response.

- [ ] **Step 1: Rename the existing exact-object test**

```python
def test_insight_evidence_response_shape_is_fixed_and_excludes_uncited_passages(
    client, auth_headers, candidate_payload, source_payload, bundle_payload
):
    ...
```

- [ ] **Step 2: Verify the renamed test passes without production changes**

Run: `python -m pytest tests/insights/test_api.py::test_insight_evidence_response_shape_is_fixed_and_excludes_uncited_passages -q`

Expected: PASS. The exact assertion continues to compare all six public fields and no uncited passage.

- [ ] **Step 3: Commit the named contract gate**

```bash
git add tests/insights/test_api.py docs/design/2026-09-16-insight-sync-contract.md
git commit -m "test: lock insight evidence response contract"
```

### Task 2: Add stable incremental store querying and cursor encoding

**Files:**
- Create: `app/services/insight_sync.py`
- Modify: `app/storage/insight_store.py:327-337`
- Modify: `tests/insights/test_store.py`
- Create: `tests/insights/test_insight_sync.py`

**Interfaces:**
- Consumes: `IntelligenceInsightRow.created_at`, `InsightRevision.created_at`, optional `datetime` boundary.
- Produces: `InsightStore.list_insights_since(since: datetime | None, after: tuple[datetime, str] | None, limit: int) -> tuple[list[InsightRevision], bool]`; `InsightListCursor`; `encode_cursor`; `decode_cursor`.

- [ ] **Step 1: Write a failing strict-boundary and same-timestamp test**

```python
def test_list_insights_since_uses_created_at_then_id_as_a_stable_boundary(store_factory):
    store = store_factory()
    older = save_fixture_insight(store, "older", datetime(2026, 9, 16, 0, 0, tzinfo=UTC))
    first = save_fixture_insight(store, "a", datetime(2026, 9, 16, 1, 0, tzinfo=UTC))
    second = save_fixture_insight(store, "b", datetime(2026, 9, 16, 1, 0, tzinfo=UTC))
    page, has_more = store.list_insights_since(older.created_at, None, limit=1)
    assert [item.insight_id for item in page] == [first.insight_id]
    page, has_more = store.list_insights_since(older.created_at, (first.created_at, first.insight_id), 1)
    assert [item.insight_id for item in page] == [second.insight_id]
    assert has_more is False
```

- [ ] **Step 2: Run the test to prove the interface is absent**

Run: `python -m pytest tests/insights/test_store.py::test_list_insights_since_uses_created_at_then_id_as_a_stable_boundary -q`

Expected: FAIL with `AttributeError` for `list_insights_since`.

- [ ] **Step 3: Add the cursor codec**

```python
@dataclass(frozen=True)
class InsightListCursor:
    since: datetime | None
    created_at: datetime
    insight_id: str

def encode_cursor(cursor: InsightListCursor) -> str: ...
def decode_cursor(value: str) -> InsightListCursor: ...
```

Use compact base64url canonical JSON. Decode exactly `since`, `created_at`, and `insight_id`; reject invalid encoding, unexpected fields, invalid timestamps, and empty IDs with `ValueError`.

- [ ] **Step 4: Add the minimal keyset query**

```python
def list_insights_since(self, since, after, limit):
    statement = select(IntelligenceInsightRow).order_by(
        IntelligenceInsightRow.created_at.asc(), IntelligenceInsightRow.insight_id.asc()
    )
    if since is not None:
        statement = statement.where(IntelligenceInsightRow.created_at > since)
    if after is not None:
        created_at, insight_id = after
        statement = statement.where(
            or_(IntelligenceInsightRow.created_at > created_at,
                and_(IntelligenceInsightRow.created_at == created_at,
                     IntelligenceInsightRow.insight_id > insight_id))
        )
    rows = session.execute(statement.limit(limit + 1)).scalars().all()
    return decoded_rows[:limit], len(rows) > limit
```

- [ ] **Step 5: Add malformed cursor coverage and verify Task 2**

```python
@pytest.mark.parametrize("value", ["not-base64", "e30", "eyJpbnNpZ2h0X2lkIjoiIn0"])
def test_decode_cursor_rejects_malformed_or_incomplete_values(value):
    with pytest.raises(ValueError):
        decode_cursor(value)
```

Run: `python -m pytest tests/insights/test_store.py tests/insights/test_insight_sync.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the store contract**

```bash
git add app/services/insight_sync.py app/storage/insight_store.py tests/insights/test_store.py tests/insights/test_insight_sync.py
git commit -m "feat: add incremental insight store query"
```

### Task 3: Expose `GET /insights?since=` and preserve search behavior

**Files:**
- Modify: `app/api/insights.py:142-145,587-597`
- Modify: `tests/insights/test_api.py`
- Modify: `docs/API_CONTRACT.md`
- Modify: `docs/design/2026-09-16-insight-sync-contract.md`

**Interfaces:**
- Consumes: Task 2 store query and cursor codec.
- Produces: `GET /insights?since=<UTC ISO-8601>&after=<opaque>&limit=<1..100>` returning `InsightSearchResults(items, next_cursor)`.

- [ ] **Step 1: Write failing endpoint tests**

```python
def test_list_insights_since_returns_a_later_replacement_and_cursor(client, auth_headers):
    original = save_api_insight(client, created_at="2026-09-16T00:00:00Z")
    replacement = save_api_insight(client, created_at="2026-09-16T01:00:00Z", supersedes_insight_id=original.insight_id)
    response = client.get("/insights?since=2026-09-16T00:30:00Z", headers=auth_headers)
    assert [item["insight_id"] for item in response.json()["items"]] == [replacement.insight_id]
    assert response.json()["next_cursor"] is None

def test_list_insights_cursor_rejects_a_different_since_boundary(client, auth_headers):
    first = client.get("/insights?since=2026-09-16T00:00:00Z&limit=1", headers=auth_headers)
    cursor = first.json()["next_cursor"]
    response = client.get(f"/insights?since=2026-09-16T00:01:00Z&after={cursor}", headers=auth_headers)
    assert response.status_code == 422
```

- [ ] **Step 2: Run the endpoint tests before implementation**

Run: `python -m pytest tests/insights/test_api.py -k 'list_insights_since or list_insights_cursor' -q`

Expected: FAIL because `since`, `after`, and a non-null cursor are absent.

- [ ] **Step 3: Implement narrow API validation and cursor response**

```python
class InsightSearchResults(BaseModel):
    items: list[InsightRevision]
    next_cursor: str | None = None

async def list_insights(
    since: datetime | None = Query(default=None),
    after: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    engine: PMEngine = Depends(get_engine),
) -> InsightSearchResults:
    ...
```

Decode `after`. When both values appear, normalize to UTC and reject inequality with `HTTPException(status_code=422, detail="cursor since boundary does not match request")`. Make a next cursor only if a final returned item exists and the store reports more rows. Do not change `/insights/search`.

- [ ] **Step 4: Document and verify the public route**

Document strict-after semantics, replacement behavior, cursor consistency, response, and 422 cases in `docs/API_CONTRACT.md`.

Run: `python -m pytest tests/insights/test_api.py tests/insights/test_store.py tests/insights/test_insight_sync.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the public API**

```bash
git add app/api/insights.py tests/insights/test_api.py docs/API_CONTRACT.md docs/design/2026-09-16-insight-sync-contract.md
git commit -m "feat: add incremental insight list API"
```

### Task 4: Verify integration and retain the control-plane boundary

**Files:**
- Modify: `docs/superpowers/plans/2026-09-16-insight-sync-contract.md`

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces: recorded checks only; no Hermes consumer or automated delivery.

- [ ] **Step 1: Run the complete bounded suite**

Run: `python -m pytest tests/insights tests/decision -q`

Expected: PASS.

- [ ] **Step 2: Run static and documentation checks**

Run: `make lint && make docs-check`

Expected: PASS.

- [ ] **Step 3: Verify OpenAPI exposes only the intended additions**

```python
parameters = client.get("/openapi.json").json()["paths"]["/insights"]["get"]["parameters"]
assert {parameter["name"] for parameter in parameters} >= {"since", "after", "limit"}
```

Expected: `since`, `after`, and `limit` are present on `/insights`; evidence still requires `revision` and its response fields remain unchanged.

- [ ] **Step 4: Record command results and commit plan evidence**

```bash
git add docs/superpowers/plans/2026-09-16-insight-sync-contract.md
git commit -m "docs: record insight sync verification"
```

## Plan Self-Review

- Spec coverage: Task 1 fixes the evidence response gate; Tasks 2-3 implement `since`, replacement semantics, keyset pagination, and error handling; Task 4 verifies the public API and no-control-plane change.
- Placeholder scan: every task names exact files, interfaces, test commands, expected results, and failure cases.
- Type consistency: `InsightListCursor`, `encode_cursor`, `decode_cursor`, and `InsightStore.list_insights_since` are defined in Task 2 and consumed with those names in Task 3.

## Execution Record

- 2026-09-16: Task 1 completed. The fixed evidence response exact-object test was renamed to state its contract and passed.
- 2026-09-16: Tasks 2 and 3 completed. Store keyset pagination, opaque cursor encode/decode, strict `since`, cursor boundary validation, and cursor-only continuation passed focused API/store tests.
- 2026-09-16: Full bounded verification passed: `python -m pytest tests/insights tests/decision -q` (94 passed), `make lint`, and `python -m mypy app`.
- 2026-09-16: `make docs-check` is not an Engine Make target; no substitute target exists in this repository. Documentation was checked with `git diff --check` and the tested API contract.
