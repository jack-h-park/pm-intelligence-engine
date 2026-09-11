import pytest

from app.s2k_fixture_replay import run_fixture_replay


@pytest.mark.asyncio
async def test_fixture_replay_completes_prepared_context_without_legacy_work_or_delivery() -> None:
    """Catch a replay path that creates a legacy signal/run or delivery receipt."""
    report = await run_fixture_replay()

    assert report["candidate_origin"] == "discovered"
    assert report["source_origin_reference"].startswith("shadow-source:")
    assert report["bundle_passage_count"] == 1
    assert report["prepared_context_validation_status"] == "valid"
    assert report["job_state"] == "complete"
    assert report["legacy_signals"] == 0
    assert report["legacy_workflow_runs"] == 0
    assert report["delivery_receipts"] == {}
