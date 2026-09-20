"""The ops turn -> engine run link: ``origin_trace_id`` (telemetry plan P3).

A run is started by an agent turn that is itself being traced. Without a shared
key the two traces are unrelatable: the engine's S1-S7 waterfall and the gateway
turn that asked for it sit in the same Langfuse project with nothing joining
them. ``origin_trace_id`` is that key -- the caller's session id, stored on the
run and set as ``langfuse.session.id`` on every span the run emits, so both
sides land under one session.

What is actually worth guarding here is the THREADING, not the column. A column
that exists but never reaches the span produces a trace that looks complete and
links to nothing, which is the failure this repo keeps getting caught by. So
these tests follow the value from the HTTP body through to the span attribute,
and pin that an omitted value stays absent rather than becoming "".
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import app.api.runs as runs_module
from app.api.deps import get_engine
from app.api.main import app
from app.factory import PMEngine
from app.models.stages import PortfolioTriageOutput, ProductRelevance
from app.services.context_loader import ContextLoader
from app.services.notifier import FanoutNotifier
from app.services.template_service import TemplateService
from app.storage.sqlite_store import SQLiteStore

TRACE = "20260920_143001_abc123"


@pytest.fixture()
def engine(tmp_path):
    store = SQLiteStore(f"sqlite:///{tmp_path}/test.db")
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value="{}")
    context_loader = MagicMock(spec=ContextLoader)
    context_loader.load_portfolio_profiles.return_value = []
    context_loader.load_pm_identity.return_value = "PM identity"
    return PMEngine(
        store=store,
        llm=llm,
        context_loader=context_loader,
        template_service=MagicMock(spec=TemplateService),
        notifier=MagicMock(spec=FanoutNotifier),
    )


@pytest.fixture()
def client(engine, monkeypatch):
    monkeypatch.setattr(runs_module, "_execute_s1_s2", AsyncMock())
    app.dependency_overrides[get_engine] = lambda: engine
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


def _seed_signal(engine: PMEngine) -> str:
    return engine.store.save_signal(
        title="Android 16 background API deprecation",
        raw_content="Some MDM background-monitoring APIs are deprecated.",
        source_type="rss",
    )


def _exists(_product_id, _engine):
    return None


# -- the value survives the round trip --------------------------------------


def test_manual_start_stores_and_returns_the_origin(client, engine, monkeypatch):
    monkeypatch.setattr(runs_module, "_validate_product_exists", _exists)
    signal_id = _seed_signal(engine)

    resp = client.post(
        "/runs/start",
        json={"signal_id": signal_id, "product_id": "prod-a", "origin_trace_id": TRACE},
    )

    assert resp.status_code == 202, resp.text
    run_id = resp.json()["run_id"]
    assert resp.json()["origin_trace_id"] == TRACE
    # ... and it is persisted, not just echoed back off the request body.
    assert engine.store.get_run(run_id)["origin_trace_id"] == TRACE
    assert client.get(f"/runs/{run_id}").json()["origin_trace_id"] == TRACE


def test_omitting_it_leaves_null_not_empty_string(client, engine, monkeypatch):
    """An untraced caller must be distinguishable from one that sent a blank.
    `""` as `langfuse.session.id` would group every such run into one session."""
    monkeypatch.setattr(runs_module, "_validate_product_exists", _exists)
    signal_id = _seed_signal(engine)

    resp = client.post("/runs/start", json={"signal_id": signal_id, "product_id": "prod-a"})

    assert resp.status_code == 202, resp.text
    assert resp.json()["origin_trace_id"] is None
    assert engine.store.get_run(resp.json()["run_id"])["origin_trace_id"] is None


def test_blank_and_whitespace_are_normalized_to_null(client, engine, monkeypatch):
    """The sending half reads an env var that may be unset, so `""` arrives on
    the wire. It means "no origin" and must not be stored as a session key."""
    monkeypatch.setattr(runs_module, "_validate_product_exists", _exists)
    signal_id = _seed_signal(engine)

    resp = client.post(
        "/runs/start",
        json={"signal_id": signal_id, "product_id": "prod-a", "origin_trace_id": "   "},
    )

    assert resp.status_code == 202, resp.text
    assert resp.json()["origin_trace_id"] is None


def test_fanout_start_tags_every_spawned_run(client, engine, monkeypatch):
    """The link must not be lost on the path the autonomous pipeline actually
    takes -- fan-out is the default start, manual is the exception."""
    signal_id = _seed_signal(engine)

    async def fake_triage(**_kwargs):
        return PortfolioTriageOutput(
            signal_id="sig",
            threshold=4,
            products=[
                ProductRelevance(product_id="prod-a", relevance_score=5, reason="r", relevant=True)
            ],
        )

    monkeypatch.setattr("app.stages.portfolio_triage.run", fake_triage)

    resp = client.post(
        "/runs/start", json={"signal_id": signal_id, "origin_trace_id": TRACE}
    )

    assert resp.status_code == 202, resp.text
    runs = resp.json()["runs"]
    assert runs, "fan-out spawned no run, so nothing was tagged"
    assert all(r["origin_trace_id"] == TRACE for r in runs)


# -- the value reaches the span ---------------------------------------------


def test_it_reaches_the_stage_span_as_the_langfuse_session(client, engine, monkeypatch):
    """The whole point of the column. Built from the store the same way the
    pipeline builds it, then run through the real span helper with a real
    in-memory exporter -- no assertion that some code 'mentions' the field."""
    sdk = pytest.importorskip("opentelemetry.sdk.trace")
    import importlib

    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    import app.telemetry as telemetry
    from app.services.run_context import load_run_context

    monkeypatch.setattr(runs_module, "_validate_product_exists", _exists)
    signal_id = _seed_signal(engine)
    run_id = client.post(
        "/runs/start",
        json={"signal_id": signal_id, "product_id": "prod-a", "origin_trace_id": TRACE},
    ).json()["run_id"]

    engine.context_loader.load_full_context.return_value = MagicMock(
        pm_identity="i", company_context="c", product_context="p"
    )
    context = load_run_context(run_id, engine)
    assert context.origin_trace_id == TRACE, "the context builder dropped it"

    importlib.reload(telemetry)
    try:
        exporter = InMemorySpanExporter()
        provider = sdk.TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        telemetry._TRACER = provider.get_tracer("test")

        with telemetry.stage_span(
            "s4", run_id, product_id="prod-a", origin_trace_id=context.origin_trace_id
        ):
            pass

        (span,) = exporter.get_finished_spans()
        assert span.attributes["langfuse.session.id"] == TRACE
    finally:
        importlib.reload(telemetry)
