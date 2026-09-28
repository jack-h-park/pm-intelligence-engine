"""Tracing must be invisible when off and honest when on.

The failure worth guarding is not "no spans" — it is a wrapper that changes what
the pipeline does. So these run real calls through the wrapper with tracing off
and assert the provider contract is untouched, then turn it on with an in-memory
exporter and assert the spans carry what a reader needs.

An assertion that `telemetry.py` mentions `usage_sink` would pass whether or not
the sink still works, which is the shape this repo's guidance calls out.
"""

from __future__ import annotations

import importlib

import pytest

from app.llm.protocol import Message, Usage


class FakeProvider:
    """Records what it was handed and reports usage the way the protocol says."""

    def __init__(self, reply: str = "ok", usage: tuple[int, int] = (11, 7), fail: bool = False):
        self.reply, self.usage, self.fail = reply, usage, fail
        self.seen: list[dict] = []

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 2048,
        temperature: float | None = None,
        usage_sink: list[Usage] | None = None,
    ) -> str:
        self.seen.append(
            {
                "messages": messages,
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        if usage_sink is not None:
            usage_sink.append({"input_tokens": self.usage[0], "output_tokens": self.usage[1]})
        if self.fail:
            raise RuntimeError("provider exploded")
        return self.reply


@pytest.fixture
def telemetry():
    """A freshly imported module, so one test's tracer never leaks into another."""
    import app.telemetry as t

    importlib.reload(t)
    yield t
    importlib.reload(t)


@pytest.fixture
def tracing_on(telemetry):
    """Real OTel spans into memory — no network, no vendor."""
    sdk = pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = sdk.TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry._TRACER = provider.get_tracer("test")
    return telemetry, exporter


# ── off: the wrapper must be transparent ─────────────────────────────────────


@pytest.mark.asyncio
async def test_off_passes_every_argument_through(telemetry):
    inner = FakeProvider(reply="hello")
    wrapped = telemetry.TracingLLMProvider(inner)

    out = await wrapped.complete(
        [{"role": "user", "content": "hi"}], model="m", max_tokens=99, temperature=0.5
    )

    assert out == "hello"
    assert inner.seen == [
        {
            "messages": [{"role": "user", "content": "hi"}],
            "model": "m",
            "max_tokens": 99,
            "temperature": 0.5,
        }
    ]


@pytest.mark.asyncio
async def test_off_leaves_the_usage_sink_contract_intact(telemetry):
    """One entry per underlying call, appended to the caller's own list."""
    wrapped = telemetry.TracingLLMProvider(FakeProvider(usage=(3, 4)))
    sink: list[Usage] = []

    await wrapped.complete([], usage_sink=sink)

    assert sink == [{"input_tokens": 3, "output_tokens": 4}]


@pytest.mark.asyncio
async def test_off_does_not_swallow_provider_errors(telemetry):
    wrapped = telemetry.TracingLLMProvider(FakeProvider(fail=True))
    with pytest.raises(RuntimeError, match="provider exploded"):
        await wrapped.complete([])


def test_off_stage_span_is_a_no_op(telemetry):
    with telemetry.stage_span("s4", "run-1"):
        pass  # must not raise with no tracer installed


def test_setup_is_off_without_keys(telemetry, monkeypatch):
    """Unsetting the PROCESS environment is not what turns tracing off here —
    the keys come from settings, so that is what this clears."""
    from config import settings

    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "", raising=False)
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "", raising=False)
    assert telemetry.setup_tracing() is False
    assert telemetry.tracing_enabled() is False


# ── on: the spans must say something useful ──────────────────────────────────


@pytest.mark.asyncio
async def test_on_still_passes_the_sink_through_to_the_caller(tracing_on):
    """The wrapper reads usage for the span; the caller must still get its own."""
    telemetry, _ = tracing_on
    wrapped = telemetry.TracingLLMProvider(FakeProvider(usage=(5, 6)))
    sink: list[Usage] = []

    await wrapped.complete([], usage_sink=sink)

    assert sink == [{"input_tokens": 5, "output_tokens": 6}]


@pytest.mark.asyncio
async def test_on_records_model_and_token_counts(tracing_on):
    telemetry, exporter = tracing_on
    wrapped = telemetry.TracingLLMProvider(FakeProvider(usage=(11, 7)))

    await wrapped.complete([], model="some-model")

    (span,) = exporter.get_finished_spans()
    assert span.name == "llm call"
    assert span.attributes["gen_ai.request.model"] == "some-model"
    assert span.attributes["gen_ai.usage.input_tokens"] == 11
    assert span.attributes["gen_ai.usage.output_tokens"] == 7
    assert span.attributes["gen_ai.usage.calls"] == 1


@pytest.mark.asyncio
async def test_on_records_fallback_model_without_claiming_missing_tokens(tracing_on):
    telemetry, exporter = tracing_on

    class FallbackProvider:
        async def complete(
            self, messages, model=None, max_tokens=2048, temperature=None, usage_sink=None
        ):
            if usage_sink is not None:
                usage_sink.append(
                    {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "model": "claude-sonnet-5",
                        "provider": "anthropic",
                        "tokens_available": False,
                    }
                )
            return "ok"

    wrapped = telemetry.TracingLLMProvider(FallbackProvider())
    await wrapped.complete([], model="gpt-6-sol")

    (span,) = exporter.get_finished_spans()
    assert span.attributes["gen_ai.request.model"] == "gpt-6-sol"
    assert span.attributes["gen_ai.response.model"] == "claude-sonnet-5"
    assert span.attributes["gen_ai.response.provider"] == "anthropic"
    assert "gen_ai.usage.input_tokens" not in span.attributes


@pytest.mark.asyncio
async def test_a_failed_call_still_reports_what_it_spent(tracing_on):
    """Tokens are burned whether or not the call returns. A span that omits them
    understates cost exactly when someone is investigating a failure."""
    telemetry, exporter = tracing_on
    wrapped = telemetry.TracingLLMProvider(FakeProvider(usage=(9, 2), fail=True))

    with pytest.raises(RuntimeError):
        await wrapped.complete([])

    (span,) = exporter.get_finished_spans()
    assert span.attributes["gen_ai.usage.input_tokens"] == 9


def test_on_stage_span_names_the_stage_and_run(tracing_on):
    telemetry, exporter = tracing_on

    with telemetry.stage_span("s4", "run-42", product_id="prod-a"):
        pass

    (span,) = exporter.get_finished_spans()
    assert span.name == "stage s4"
    assert span.attributes["pm.stage"] == "s4"
    assert span.attributes["pm.run_id"] == "run-42"
    assert span.attributes["pm.product_id"] == "prod-a"


def test_stage_span_lets_the_stage_error_through(tracing_on):
    telemetry, exporter = tracing_on

    with pytest.raises(ValueError):
        with telemetry.stage_span("s2", "run-1"):
            raise ValueError("stage failed")

    (span,) = exporter.get_finished_spans()
    assert span.name == "stage s2"


def test_stage_span_carries_the_origin_as_the_langfuse_session(tracing_on):
    """`langfuse.session.id` is what groups the engine's run trace with the ops
    turn that started it. The key is the vendor's documented OTel attribute, so
    it is pinned here rather than left to whichever name reads nicely."""
    telemetry, exporter = tracing_on

    with telemetry.stage_span("s4", "run-42", origin_trace_id="20260920_143001_abc123"):
        pass

    (span,) = exporter.get_finished_spans()
    assert span.attributes["langfuse.session.id"] == "20260920_143001_abc123"


def test_stage_span_omits_the_session_when_there_is_no_origin(tracing_on):
    """An untraced start must leave the attribute ABSENT. Writing "" would put
    every such run in one shared Langfuse session -- a grouping that looks real
    and joins unrelated runs."""
    telemetry, exporter = tracing_on

    with telemetry.stage_span("s4", "run-42", origin_trace_id=None):
        pass

    (span,) = exporter.get_finished_spans()
    assert "langfuse.session.id" not in span.attributes


# ── the keys have to come from where the engine keeps configuration ──────────
# The service is launched by launchd with only LANG and PATH in its environment
# and no wrapper: `.env` reaches the process through pydantic-settings, which
# reads the FILE and never exports to `os.environ`. Read on the ops host with the
# service's own interpreter and working directory:
#
#     os.environ has LANGFUSE_PUBLIC_KEY: False
#     os.environ has OPENAI_API_KEY    : False
#     settings sees OPENAI_API_KEY     : True
#
# So an `os.environ` lookup here finds nothing however correct `.env` is, and
# tracing fails open — the symptom is zero traces and no error, on a deployment
# that looks fully configured. These drive the real function against settings.


def test_setup_reads_the_keys_from_settings_not_the_process_environment(telemetry, monkeypatch):
    """With keys in settings and NOTHING in os.environ, setup must still see them.

    It cannot complete without the optional dependencies, so this asserts on how
    far it gets: past the key gate. `_keys()` is that gate.
    """
    from config import settings

    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "pk-from-settings", raising=False)
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "sk-from-settings", raising=False)

    assert telemetry._keys() == ("pk-from-settings", "sk-from-settings")


def test_blank_settings_leave_tracing_off(telemetry, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "  ", raising=False)
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "", raising=False)

    assert telemetry._keys() == ("", "")
    assert telemetry.setup_tracing() is False
    assert telemetry.tracing_enabled() is False


def test_the_host_is_configurable_and_defaults_to_nothing_implicit(telemetry):
    """The vendor client defaults to its EU endpoint when no host is given, and
    these projects are on US — a default that silently fails to authenticate.
    The engine therefore has to pass the host it was configured with, and
    `.env.example` documents it."""
    from config import settings

    assert hasattr(settings, "LANGFUSE_HOST")
    # No implicit default: an unset host must not quietly become a region.
    assert type(settings).model_fields["LANGFUSE_HOST"].default == ""
