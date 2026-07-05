"""Clean-model behavior: `note` depth completes at S2, it does not run S7.

Under the (position, lifecycle) model a run stops AT its target position; `note`'s
target is S2, whose insight memo is the artifact. The old behavior jumped to S7 to
render a half-"not run" summary — an anomaly the linear position model removes
(WORKFLOW_MODEL_REDESIGN.md §6 / US-53). This locks the new behavior end-to-end.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_engine
from app.api.main import app
from app.factory import PMEngine
from app.services.context_loader import ContextLoader, FullContext
from app.services.notifier import FanoutNotifier
from app.services.template_service import TemplateService
from app.storage.sqlite_store import SQLiteStore
from tests.integration.conftest import run_status, seed_run_state


@pytest.fixture()
def engine(tmp_path):
    store = SQLiteStore(f"sqlite:///{tmp_path}/test.db")
    context_loader = MagicMock(spec=ContextLoader)
    context_loader.load_full_context.return_value = FullContext(
        pm_identity="pm", company_context="co", product_context="prod",
        product_id="example-security-product",
    )
    notifier = MagicMock(spec=FanoutNotifier)
    notifier.send_gate1 = AsyncMock()
    notifier.send_gate2 = AsyncMock()
    return PMEngine(
        store=store, llm=AsyncMock(),
        context_loader=context_loader,
        template_service=MagicMock(spec=TemplateService),
        notifier=notifier,
    )


@pytest.fixture()
def client(engine):
    app.dependency_overrides[get_engine] = lambda: engine
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


_S2_OUTPUT = {
    "stage": "s2", "version": 1,
    "output": {
        "what_changed": "Something changed.",
        "reframing": "market vs product framing",
        "pillar_references": ["Pillar 1"],
        "claims": [{"text": "A fact.", "source": "signal", "grounds": []},
                   {"text": "A conclusion.", "source": "inference", "grounds": [1]}],
        "relevance_score": 4, "suggested_mode": "note",
        "suggestion_reasoning": "Worth recording.",
    },
}


def _seed_waiting_direction(engine: PMEngine) -> str:
    signal_id = engine.store.save_signal(
        original_product_id="example-security-product",
        title="Note-depth signal", raw_content="Body.",
    )
    run_id = engine.store.create_run("example-security-product", signal_id)
    engine.store.save_stage_output(run_id, "s2", json.dumps(dict(_S2_OUTPUT, run_id=run_id)))
    seed_run_state(engine.store, run_id, "waiting_direction")
    return run_id


def test_note_direction_completes_at_s2_without_s7(client, engine):
    run_id = _seed_waiting_direction(engine)

    resp = client.post(f"/runs/{run_id}/direction", json={"depth": "note"})
    assert resp.status_code == 202, resp.text

    run = engine.store.get_run(run_id)
    assert run_status(run) == "completed"
    assert run["mode"] == "note"
    assert run["ended_by"] == "noted"

    # The clean-model assertion: S7 never ran, so there is no S7 output and no
    # executive_summary artifact — the S2 insight memo is the deliverable.
    assert engine.store.get_stage_output(run_id, "s7") is None
    assert engine.store.list_artifacts(run_id, artifact_type="executive_summary") == []
