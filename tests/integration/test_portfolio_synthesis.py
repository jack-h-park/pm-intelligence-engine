"""Tests for Portfolio Synthesis (US-49, Variant 2) — service + finalize trigger."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.factory import PMEngine
from app.services.context_loader import ContextLoader
from app.services.notifier import FanoutNotifier
from app.services.template_service import TemplateService
from app.storage.sqlite_store import SQLiteStore
from tests.integration.conftest import run_status, seed_run_state

_SYNTH_JSON = json.dumps({
    "priority_ranking": [
        {"product_id": "prod-a", "rank": 1, "rationale": "direct hit"},
        {"product_id": "prod-b", "rank": 2, "rationale": "secondary"},
    ],
    "shared_root_cause": "Same deprecated API.",
    "sequencing": "A before B.",
    "resource_conflicts": "Both need Q3 platform capacity.",
    "synergies": "Shared monitoring layer.",
    "recommendation": "Hold A and B as one response.",
})


@pytest.fixture(autouse=True)
def _stub_prompt(monkeypatch):
    # Don't depend on the real decision-context checkout for the prompt text.
    monkeypatch.setattr(
        "app.services.template_service.TemplateService.load_portfolio_prompt",
        lambda self, kind: "(synthesis framework)",
    )


@pytest.fixture()
def engine(tmp_path):
    store = SQLiteStore(f"sqlite:///{tmp_path}/test.db")
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=_SYNTH_JSON)
    context_loader = MagicMock(spec=ContextLoader)
    context_loader.load_pm_identity.return_value = "PM identity"
    return PMEngine(
        store=store,
        llm=llm,
        context_loader=context_loader,
        template_service=MagicMock(spec=TemplateService),
        notifier=MagicMock(spec=FanoutNotifier),
    )


def _batch_with_runs(engine, n=2, *, close=True, settle=True) -> tuple[str, list[str]]:
    signal_id = engine.store.save_signal(title="Android 16 API change", raw_content="…")
    batch_id = engine.store.create_batch(signal_id)
    run_ids = []
    for i in range(n):
        rid = engine.store.create_run(f"prod-{chr(97 + i)}", signal_id, batch_id=batch_id)
        if settle:
            seed_run_state(engine.store, rid, "completed")
        run_ids.append(rid)
    if close:
        engine.store.close_batch_membership(batch_id)
    return batch_id, run_ids


# --- service: synthesize_batch -------------------------------------------------

@pytest.mark.asyncio
async def test_synthesize_writes_memo_once(engine):
    from app.services.portfolio_synthesis import synthesize_batch

    batch_id, _ = _batch_with_runs(engine, n=2)
    assert await synthesize_batch(batch_id, engine) is True

    stored = engine.store.get_portfolio_synthesis(batch_id)
    assert stored is not None
    assert "Portfolio Synthesis" in stored["content_md"]
    assert "prod-a" in stored["content_md"]
    assert json.loads(stored["content_json"])["shared_root_cause"] == "Same deprecated API."

    # idempotent: a second call writes nothing new
    engine.llm.complete.reset_mock()
    assert await synthesize_batch(batch_id, engine) is False


@pytest.mark.asyncio
async def test_synthesize_skips_single_run_batch(engine):
    from app.services.portfolio_synthesis import synthesize_batch

    batch_id, _ = _batch_with_runs(engine, n=1)
    assert await synthesize_batch(batch_id, engine) is False
    engine.llm.complete.assert_not_called()


# --- readiness guards ----------------------------------------------------------

def test_ready_guards(engine):
    from app.services.portfolio_synthesis import batch_ready_for_synthesis

    # open membership -> not ready
    open_batch, _ = _batch_with_runs(engine, n=2, close=False)
    assert batch_ready_for_synthesis(open_batch, engine) is False

    # a run still unsettled -> not ready
    unsettled, _ = _batch_with_runs(engine, n=2, settle=False)
    assert batch_ready_for_synthesis(unsettled, engine) is False

    # single run -> not ready
    single, _ = _batch_with_runs(engine, n=1)
    assert batch_ready_for_synthesis(single, engine) is False

    # closed + multi + all settled -> ready
    ready, _ = _batch_with_runs(engine, n=2)
    assert batch_ready_for_synthesis(ready, engine) is True


# --- finalize_run trigger ------------------------------------------------------

@pytest.mark.asyncio
async def test_finalize_triggers_only_when_batch_complete(engine):
    from app.services.run_finalizer import finalize_run

    signal_id = engine.store.save_signal(title="S", raw_content="…")
    batch_id = engine.store.create_batch(signal_id)
    r1 = engine.store.create_run("prod-a", signal_id, batch_id=batch_id)
    r2 = engine.store.create_run("prod-b", signal_id, batch_id=batch_id)
    engine.store.close_batch_membership(batch_id)

    # first sibling settles — batch not yet complete, no synthesis
    await finalize_run(r1, "completed", engine)
    assert engine.store.get_portfolio_synthesis(batch_id) is None

    # last sibling settles — synthesis fires
    await finalize_run(r2, "killed", engine)
    assert engine.store.get_portfolio_synthesis(batch_id) is not None


@pytest.mark.asyncio
async def test_finalize_single_run_batch_no_synthesis(engine):
    from app.services.run_finalizer import finalize_run

    # a run with no batch_id (manual single start) never synthesizes
    signal_id = engine.store.save_signal(title="S", raw_content="…")
    run_id = engine.store.create_run("prod-a", signal_id)
    await finalize_run(run_id, "completed", engine)
    # nothing to assert beyond "did not raise / no batch row created"
    assert run_status(engine.store.get_run(run_id)) == "completed"
