# Task 1 Report: Deterministic Paragraph Evidence Preparation

## Status

Implemented and verified. The evidence preparation service now splits source bodies on blank-line paragraph boundaries, preserves source order and the original locator, and emits stable `source_id:index` passage identifiers. Extraction remains local, deterministic, bounded, and does not fetch, summarize, model-process, assign products, create decision cases, run workflows, notify, or mutate existing records.

## Changes

- Added `MAX_PASSAGES_PER_SOURCE = 12`.
- Added deterministic paragraph extraction using `re.split(r"\n\s*\n", material)`.
- Retained the prescribed fallback behavior for a body with no splittable paragraphs.
- Added a coarse-passage coverage gap for unsplittable non-empty source bodies.
- Added a coverage gap when later paragraphs are omitted by the per-source limit.
- Preserved existing usable-source selection (`ok`, `fallback_summary`), four-source cap, seed/enrichment roles, source IDs, and locator behavior.
- Added focused tests for ordered paragraph passages and coarse single-body coverage.

## Verification

- `python -m pytest tests/insights/test_evidence.py -q`: **4 passed**.
- `python -m pytest tests/insights -q`: **54 passed**.
- `ruff check app/services/insight_evidence.py tests/insights/test_evidence.py`: **passed**.
- `git diff --check`: **passed**.

The repository-wide `python -m pytest -q` run reached unrelated integration/configuration failures outside the evidence tests; the complete `tests/insights` suite remains green.

## Commit

`feat: split insight evidence into passages` (committed on the task branch).

## Review Follow-up

Corrected coarse-passage disclosure to use the parsed count of non-empty paragraphs rather than the presence of a raw blank-line delimiter. Added regression coverage for a single paragraph with trailing blank whitespace (`"Only paragraph.\\n\\n"`).

- RED: the new regression failed with an empty `coverage_gaps` list.
- GREEN: `python -m pytest tests/insights/test_evidence.py -q`: **5 passed**.
- Full focused suite: `python -m pytest tests/insights -q`: **55 passed**.
- `ruff check app/services/insight_evidence.py tests/insights/test_evidence.py`: **passed**.
- `git diff --check`: **passed**.
