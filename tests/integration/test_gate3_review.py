"""Integration tests for the enriched Gate 3 payload (US-30).

GET /runs/{id} exposes gate3_review (assumptions with severity, per-persona
score+argument summaries, S4 rubric total) once S5 has run, so the PM can
confirm or override routing without opening the database.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_engine
from app.api.main import app
from app.factory import PMEngine
from app.services.context_loader import ContextLoader
from app.services.notifier import FanoutNotifier
from app.services.template_service import TemplateService
from app.storage.sqlite_store import SQLiteStore


@pytest.fixture()
def engine(tmp_path):
    db_url = f"sqlite:///{tmp_path}/test.db"
    store = SQLiteStore(db_url)
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value="{}")

    context_loader = MagicMock(spec=ContextLoader)
    template_service = MagicMock(spec=TemplateService)
    notifier = MagicMock(spec=FanoutNotifier)
    notifier.send_gate3 = AsyncMock()

    return PMEngine(
        store=store,
        llm=llm,
        context_loader=context_loader,
        template_service=template_service,
        notifier=notifier,
    )


@pytest.fixture()
def client(engine):
    app.dependency_overrides[get_engine] = lambda: engine
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


_S4_OUTPUT = {
    "stage": "s4",
    "run_id": "placeholder",
    "version": 1,
    "output": {
        "personas": [
            {"persona": "explorer", "dimension": "Impact", "score": 4,
             "key_argument": "Opens adjacent market.", "open_question": "Customer interview."},
            {"persona": "strategist", "dimension": "Strategic Fit", "score": 5,
             "key_argument": "Reinforces pillar 1.", "open_question": "Strategy review."},
            {"persona": "builder", "dimension": "Feasibility", "score": 4,
             "key_argument": "Two-quarter build.", "open_question": "Engineering spike."},
            {"persona": "skeptic", "dimension": "Confidence", "score": 3,
             "key_argument": "Adoption assumption untested.", "open_question": "Customer survey."},
        ],
        "rubric": {
            "total_score": 11, "score_grounding": 3, "skeptic_quality": 3,
            "open_question_quality": 2, "persona_independence": 3,
            "passed": True, "issues": [],
        },
    },
}

_S5_OUTPUT = {
    "stage": "s5",
    "run_id": "placeholder",
    "version": 1,
    "output": {
        "impact_score": 4, "strategic_fit_score": 5, "feasibility_score": 4,
        "confidence_score": 3, "composite_score": 4.3, "routing": "poc",
        "assumptions": [
            {"statement": "Admins want unified enforcement", "severity": "Informing",
             "reason": "Scope narrows if false"},
            {"statement": "Platform API ships in GA", "severity": "Blocking",
             "reason": "No product without it"},
        ],
        "blocking_count": 1,
        "rationale": "Strong composite, unvalidated confidence -> poc.",
    },
}


def _seed_run_with_s4_s5(engine: PMEngine) -> str:
    signal_id = engine.store.save_signal(
        product_id="example-security-product", title="Sig", raw_content="Text."
    )
    run_id = engine.store.create_run("example-security-product", signal_id)
    engine.store.update_run(
        run_id, status="waiting_routing_review", current_stage="s5",
        mode="decide", routing="poc",
    )
    for stage, payload in (("s4", _S4_OUTPUT), ("s5", _S5_OUTPUT)):
        body = dict(payload, run_id=run_id)
        engine.store.save_stage_output(
            run_id=run_id, stage=stage, output_json=json.dumps(body)
        )
    return run_id


def test_gate3_review_present_after_s5(client, engine):
    run_id = _seed_run_with_s4_s5(engine)
    resp = client.get(f"/runs/{run_id}")
    assert resp.status_code == 200
    review = resp.json()["gate3_review"]

    assert review["routing"] == "poc"
    assert review["composite_score"] == 4.3
    assert review["blocking_count"] == 1
    assert review["rubric_total"] == "11/12"
    assert review["rationale"]

    severities = {a["statement"]: a["severity"] for a in review["assumptions"]}
    assert severities["Platform API ships in GA"] == "Blocking"
    assert severities["Admins want unified enforcement"] == "Informing"

    personas = {p["persona"]: p for p in review["personas"]}
    assert len(personas) == 4
    assert personas["skeptic"]["score"] == 3
    assert personas["skeptic"]["key_argument"] == "Adoption assumption untested."


def test_gate3_review_absent_before_s5(client, engine):
    signal_id = engine.store.save_signal(
        product_id="example-security-product", title="Sig", raw_content="Text."
    )
    run_id = engine.store.create_run("example-security-product", signal_id)
    resp = client.get(f"/runs/{run_id}")
    assert resp.status_code == 200
    assert resp.json()["gate3_review"] is None
