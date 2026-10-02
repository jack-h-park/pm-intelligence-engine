"""An Insight job must read as one trace in its Candidate's session.

Before this the worker was the engine's main model consumer and emitted nothing:
its provider was built outside the tracing wrapper and its stages opened no
spans. These run the real worker against a real store with an in-memory
exporter and assert on the tree that comes out -- names, parents and the
session key -- rather than on the worker mentioning a span helper.
"""

from __future__ import annotations

import pytest

import app.insight_worker as insight_worker
import app.telemetry as telemetry
from app.insight_worker import process_one

ANALYSIS = """{
  "headline": "A fixture insight", "explanation": "Bounded evidence.",
  "actual_change": "A source was supplied.", "why_now": "The job is queued.",
  "personal_relevance": "It answers the question.", "takeaway": "Test it.",
  "claims": [{"text": "The source was supplied.", "passage_ids": ["passage-fixture-1"]}],
  "uncertainties": []
}"""


class FixtureLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, **kwargs):
        self.calls += 1
        return ANALYSIS


@pytest.fixture(autouse=True)
def worker_context(tmp_path, monkeypatch):
    from config import settings

    root = tmp_path / "worker-context"
    (root / "core").mkdir(parents=True)
    (root / "core/signal-interest-context.yaml").write_text(
        "revision: fixture-context-v1\ninterests:\n"
        "  - id: android-enterprise-isolation\n"
        "    question: What changed in managed profile isolation?\n"
        "    constraints: [Preserve attribution.]\n"
    )
    monkeypatch.setattr(settings, "DECISION_CONTEXT_ROOT", str(root))
    monkeypatch.setattr(settings, "INSIGHT_PROJECTION_ENABLED", False)
    monkeypatch.setattr(settings, "INSIGHT_PRODUCT_RELEVANCE_ENABLED", False)


@pytest.fixture
def exporter(monkeypatch):
    sdk = pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    memory = InMemorySpanExporter()
    provider = sdk.TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    monkeypatch.setattr(telemetry, "_TRACER", provider.get_tracer("test"))
    return memory


def _queued_job(store_factory, candidate_payload, source_payload, bundle_payload):
    store = store_factory()
    candidate = store.save_candidate(candidate_payload)
    source = store.save_source({**source_payload, "candidate_id": candidate.candidate_id})
    bundle = store.save_bundle(bundle_payload(candidate.candidate_id, source.source_id))
    job = store.create_job(
        {
            "candidate_id": candidate.candidate_id,
            "context_revision": "fixture-context-v1",
            "purpose": "learning",
            "bundle_id": bundle.bundle_id,
        }
    )
    return store, candidate, job


@pytest.mark.asyncio
async def test_a_job_is_one_trace_with_its_stages_and_calls_nested(
    exporter, store_factory, candidate_payload, source_payload, bundle_payload
):
    store, candidate, job = _queued_job(
        store_factory, candidate_payload, source_payload, bundle_payload
    )
    llm = FixtureLLM()

    completed = await process_one(store, telemetry.TracingLLMProvider(llm))

    assert completed is not None and llm.calls >= 1
    spans = {span.name: span for span in exporter.get_finished_spans()}
    root = spans["insight job"]
    assert root.parent is None
    assert root.attributes["pm.insight.job_id"] == job.job_id
    assert root.attributes["pm.insight.candidate_id"] == candidate.candidate_id

    session = f"insight:{candidate.candidate_id}"
    for name in ("insight job", "insight analysis", "insight knowledge verdict"):
        assert spans[name].attributes["langfuse.session.id"] == session, name
    for name in ("insight analysis", "insight knowledge verdict"):
        assert spans[name].parent.span_id == root.context.span_id, name

    stage_ids = {
        spans[name].context.span_id for name in ("insight analysis", "insight knowledge verdict")
    }
    calls = [span for span in exporter.get_finished_spans() if span.name == "llm call"]
    assert len(calls) == llm.calls
    assert all(call.parent is not None and call.parent.span_id in stage_ids for call in calls), (
        "a model call outside a stage span is a parentless trace nobody can place"
    )
    # One trace: every span shares the root's trace id.
    assert {span.context.trace_id for span in exporter.get_finished_spans()} == {
        root.context.trace_id
    }
    # The switch is off, so the stage that did not run left no empty span.
    assert "insight product relevance" not in spans


@pytest.mark.asyncio
async def test_a_job_that_calls_no_model_leaves_no_trace(
    exporter, store_factory, candidate_payload
):
    """Needs-evidence and no-new-learning exits happen before any model call."""
    store = store_factory()
    candidate = store.save_candidate(candidate_payload)
    store.create_job(
        {
            "candidate_id": candidate.candidate_id,
            "context_revision": "fixture-context-v1",
            "purpose": "learning",
            "bundle_id": None,
        }
    )
    llm = FixtureLLM()

    assert await process_one(store, telemetry.TracingLLMProvider(llm)) is None
    assert llm.calls == 0
    assert exporter.get_finished_spans() == ()


@pytest.mark.asyncio
async def test_the_bridge_is_still_found_underneath_the_tracing_wrapper(
    monkeypatch, store_factory, candidate_payload, source_payload, bundle_payload
):
    """The worker binds the job and sizes its lease from the S2K bridge's own
    methods. Wrapping the provider at the factory must not hide them: an
    `isinstance` check on the wrapper is False, the job id is never bound and
    the lease is never extended -- with no error anywhere."""

    class Bridge(FixtureLLM):
        completion_timeout_seconds = 5.0

        def __init__(self) -> None:
            super().__init__()
            self.bound: list[str] = []

        def bind_job_id(self, job_id: str) -> None:
            self.bound.append(job_id)

    monkeypatch.setattr(insight_worker, "S2KBridgeProvider", Bridge)
    store, _, job = _queued_job(store_factory, candidate_payload, source_payload, bundle_payload)
    started: list[float] = []
    real = store.mark_job_inference_started

    def record(job_id, token, *, lease_seconds):
        started.append(lease_seconds)
        return real(job_id, token, lease_seconds=lease_seconds)

    monkeypatch.setattr(store, "mark_job_inference_started", record)
    bridge = Bridge()

    await process_one(store, telemetry.TracingLLMProvider(bridge))

    assert bridge.bound == [job.job_id]
    assert started and started[0] >= 120.0


def test_the_factory_wraps_the_bridge_at_its_one_construction_point(monkeypatch):
    import app.factory as factory
    import app.llm.s2k_bridge as bridge_module

    sentinel = object()
    monkeypatch.setattr(
        bridge_module, "build_s2k_llm_provider", lambda operation_id=None: sentinel
    )

    built = factory.build_s2k_llm_provider("op-1")

    assert isinstance(built, telemetry.TracingLLMProvider)
    assert telemetry.unwrap_provider(built) is sentinel
