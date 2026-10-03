"""OpenTelemetry spans for pipeline runs, exported to Langfuse when configured.

Off by default. With no exporter configured every helper here is a no-op that
costs a dict lookup, so an unconfigured deployment — which is every deployment
until someone sets the keys — behaves exactly as it did before.

Why plain OpenTelemetry rather than an LLM-observability SDK's own decorators:
the spans this emits are ordinary OTel spans, and the vendor only appears once,
in ``setup_tracing()``, where its span processor is attached. Swapping or
removing the backend is that one function; the stage and provider code never
names it. That also keeps this service's stated provider-neutrality intact --
the dependency at the call site is ``opentelemetry``, not a vendor.

Three kinds of span are emitted, and no more:

* one per pipeline stage, around ``run_stage`` and around the S1/S2 pair that
  ``_execute_s1_s2`` runs outside it
* one per Insight job, with one child per worker stage (analysis, knowledge
  verdict, product relevance)
* one per LLM call, from a wrapper around whatever ``LLMProvider`` a factory
  built -- the configured provider and the S2K bridge alike

Grouping: every span that belongs to one Candidate carries the same
``langfuse.session.id`` (``insight_session``), so the worker's analysis and a
decision run later made from that Insight read as one session. The engine sets
it alone -- it holds both ends -- and nothing is propagated from a caller.

The provider span is a wrapper rather than an edit inside each provider on
purpose. ``LLMProvider`` is a Protocol with several implementations, and
instrumenting each one is the dual-implementation trap: the copies agree until
someone changes one. ``_build_llm_provider()`` is a single construction point,
so wrapping there instruments every provider, including ones added later.

Failure policy: telemetry never breaks a run. Setup catches and logs; the span
helpers are context managers that let the wrapped code's own exception through
untouched after recording it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.llm.protocol import LLMProvider, Message, Usage

logger = logging.getLogger(__name__)

_TRACER: Any | None = None

# Named explicitly because the project this exports to is shared with the agent
# fleet: without it the SDK reports ``unknown_service`` and nothing on a trace
# says it came from the engine.
SERVICE_NAME = "pm-intelligence-engine"


def insight_session(candidate_id: str | None) -> str | None:
    """The session key for everything descended from one Candidate, or None.

    None rather than a placeholder when there is no Candidate: see ``stage_span``
    on why an empty or shared key is worse than none.
    """
    candidate_id = (candidate_id or "").strip()
    return f"insight:{candidate_id}" if candidate_id else None


def tracing_enabled() -> bool:
    return _TRACER is not None


def _keys() -> tuple[str, str]:
    """The configured key pair, or a pair of empty strings.

    Read through ``config.settings`` and not ``os.environ``: launchd starts this
    service with only LANG and PATH, so `.env` arrives via pydantic-settings,
    which reads the file without exporting it. Checked BEFORE any optional
    import, so a host without the telemetry extra still starts.
    """
    from config import settings

    return (
        (getattr(settings, "LANGFUSE_PUBLIC_KEY", "") or "").strip(),
        (getattr(settings, "LANGFUSE_SECRET_KEY", "") or "").strip(),
    )


def _export_filter(default: Any) -> Any:
    """Export this service's own spans, plus whatever the vendor would by default.

    The vendor's default filter keeps only its own SDK spans, spans carrying a
    ``gen_ai.*`` attribute, and known LLM instrumentation scopes. Every stage and
    Insight-job span here is none of those, so under the default they were
    dropped at export while the ``llm call`` generations beneath them survived:
    the backend showed parentless generations with no session, because the
    session lives on the dropped parents. In-memory exporter tests cannot see
    this -- the filter is applied by the vendor's processor, not by OTel.
    """

    def should_export(span: Any) -> bool:
        scope = getattr(span, "instrumentation_scope", None)
        if scope is not None and scope.name == SERVICE_NAME:
            return True
        return bool(default(span))

    return should_export


def setup_tracing() -> bool:
    """Attach an exporter if one is configured. Returns whether tracing is on.

    Idempotent, and safe to call when the optional dependencies are absent: a
    missing package or a missing key leaves tracing off rather than failing
    startup. The service's job is to run pipelines, not to export spans.
    """
    global _TRACER
    if _TRACER is not None:
        return True

    from app.logging import emit_event
    from config import settings

    public_key, secret_key = _keys()
    if not (public_key and secret_key):
        # Recorded rather than silent: an operator should not have to infer the
        # state from the absence of a line.
        emit_event("telemetry", "tracing_off_no_keys", "-")
        return False

    try:
        from langfuse import Langfuse, is_default_export_span
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
    except ImportError as exc:
        logger.warning(
            "Tracing keys are set but the optional dependencies are missing "
            "(%s). Install the 'telemetry' extra to enable it.",
            exc,
        )
        return False

    try:
        # Building the client is what registers the span processor; the
        # returned handle is deliberately unused here, because everything this
        # module emits goes through the OTel API rather than the vendor's.
        #
        # The credentials are PASSED rather than left to the client's own env
        # lookup: it reads `os.environ`, which this service's `.env` never
        # reaches (see config.py). Host likewise — the client's default is its
        # EU endpoint, and a US project does not authenticate there.
        kwargs: dict[str, Any] = {"public_key": public_key, "secret_key": secret_key}
        if getattr(settings, "LANGFUSE_HOST", ""):
            kwargs["host"] = settings.LANGFUSE_HOST.strip()
        if getattr(settings, "LANGFUSE_TIMEOUT", 0):
            kwargs["timeout"] = settings.LANGFUSE_TIMEOUT
        # The provider is built HERE and handed to the client, rather than left
        # for the client to create: the resource (and so `service.name`) is fixed
        # when a provider is constructed, and the client's own carries none.
        provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
        kwargs["tracer_provider"] = provider
        kwargs["should_export_span"] = _export_filter(is_default_export_span)
        Langfuse(**kwargs)
        _TRACER = provider.get_tracer(SERVICE_NAME)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Tracing setup failed (%s); continuing without it.", exc)
        return False

    # Structured, not logger.info: under uvicorn the app loggers sit at WARNING,
    # so the one line that tells an operator tracing came up was swallowed — the
    # service log showed a clean startup whether tracing was on or off, which is
    # the state this module's whole failure mode hides in. `emit_event` is the
    # channel the notifier's own startup line uses, and that one does appear.
    emit_event(
        "telemetry",
        "tracing_enabled",
        "-",
        {
            "host": getattr(settings, "LANGFUSE_HOST", "") or "vendor default",
            "timeout_s": getattr(settings, "LANGFUSE_TIMEOUT", 0) or "sdk default",
        },
    )
    return True


@contextmanager
def stage_span(
    position: str,
    run_id: str,
    product_id: str | None = None,
    origin_trace_id: str | None = None,
) -> Iterator[None]:
    """Wrap one pipeline stage. A no-op when tracing is off.

    ``origin_trace_id`` is the telemetry session of whoever started the run. It
    is set as ``langfuse.session.id`` -- the backend's documented OTel attribute
    for session grouping -- so the run's spans and the agent turn that asked for
    the run land under one session. Vendor-named, but still a plain OTel
    attribute: a backend that does not know the key ignores it.

    It is set per span rather than once per run because each stage span is its
    own root; a session recorded only on the first one would leave every later
    stage ungrouped.
    """
    if _TRACER is None:
        yield
        return
    with _TRACER.start_as_current_span(f"stage {position}") as span:
        span.set_attribute("pm.stage", position)
        span.set_attribute("pm.run_id", run_id)
        if product_id:
            span.set_attribute("pm.product_id", product_id)
        # Left ABSENT rather than "" when there is no origin: an empty session id
        # is a real grouping key, and would collect every untraced run into one
        # session that looks meaningful and is not.
        if origin_trace_id:
            span.set_attribute("langfuse.session.id", origin_trace_id)
        yield


@contextmanager
def insight_job_span(job_id: str, candidate_id: str | None) -> Iterator[None]:
    """Wrap one claimed Insight job: the root its worker stages hang under.

    A no-op when tracing is off. One trace per job, so a retried job is a second
    trace in the same session rather than a longer first one.
    """
    if _TRACER is None:
        yield
        return
    with _TRACER.start_as_current_span("insight job") as span:
        span.set_attribute("pm.insight.job_id", job_id)
        if candidate_id:
            span.set_attribute("pm.insight.candidate_id", candidate_id)
        session = insight_session(candidate_id)
        if session:
            span.set_attribute("langfuse.session.id", session)
        yield


@contextmanager
def insight_stage_span(stage: str, candidate_id: str | None = None) -> Iterator[None]:
    """Wrap one worker stage inside ``insight_job_span``. A no-op when off.

    The session is repeated here, not inherited: a stage can also be reached
    with no job span above it (a backfill or a scoped re-run entered directly),
    and it must still land in its Candidate's session.
    """
    if _TRACER is None:
        yield
        return
    with _TRACER.start_as_current_span(f"insight {stage}") as span:
        span.set_attribute("pm.insight.stage", stage)
        session = insight_session(candidate_id)
        if session:
            span.set_attribute("langfuse.session.id", session)
        yield


def unwrap_provider(llm: Any) -> Any:
    """The provider underneath any tracing wrapper.

    For the few callers that need the concrete type -- the Insight worker sizes
    its lease from the S2K bridge's own timeout. Calls still go through the
    wrapper; only the type check looks underneath.
    """
    while isinstance(llm, TracingLLMProvider):
        llm = llm.inner
    return llm


class TracingLLMProvider:
    """An ``LLMProvider`` that emits one span per call and delegates the rest.

    Wraps any provider rather than editing each one -- see this module's
    docstring. Token counts come from ``usage_sink``, which the protocol already
    defines: a local sink is always passed down so the span can read the counts
    even when the caller asked for none, and the caller's own sink still
    receives exactly the entries it would have without this wrapper.
    """

    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner

    @property
    def inner(self) -> LLMProvider:
        return self._inner

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 2048,
        temperature: float | None = None,
        usage_sink: list[Usage] | None = None,
    ) -> str:
        if _TRACER is None:
            return await self._inner.complete(
                messages,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                usage_sink=usage_sink,
            )

        local: list[Usage] = []
        with _TRACER.start_as_current_span("llm call") as span:
            if model:
                span.set_attribute("gen_ai.request.model", model)
            span.set_attribute("gen_ai.request.max_tokens", max_tokens)
            try:
                result = await self._inner.complete(
                    messages,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    usage_sink=local,
                )
                # A provider that cannot meter tokens appends nothing to the
                # sink, and would leave the span naming no model at all. The
                # response's own route says which one answered either way.
                route = getattr(result, "route", None)
                if route is not None and not local:
                    span.set_attribute("gen_ai.response.model", route.model)
                    span.set_attribute("gen_ai.response.provider", route.provider)
                return result
            finally:
                # In `finally` so a failed call still reports what it spent:
                # a provider that raises after two retries has still burned
                # those tokens, and a span that omits them understates cost
                # exactly when someone is looking into a failure.
                if local:
                    models = list(dict.fromkeys(u["model"] for u in local if u.get("model")))
                    providers = list(
                        dict.fromkeys(u["provider"] for u in local if u.get("provider"))
                    )
                    if models:
                        span.set_attribute("gen_ai.response.model", ",".join(models))
                    if providers:
                        span.set_attribute("gen_ai.response.provider", ",".join(providers))
                    metered = [u for u in local if u.get("tokens_available", True)]
                    if metered:
                        span.set_attribute(
                            "gen_ai.usage.input_tokens", sum(u["input_tokens"] for u in metered)
                        )
                        span.set_attribute(
                            "gen_ai.usage.output_tokens", sum(u["output_tokens"] for u in metered)
                        )
                    span.set_attribute("gen_ai.usage.calls", len(local))
                if usage_sink is not None:
                    usage_sink.extend(local)
