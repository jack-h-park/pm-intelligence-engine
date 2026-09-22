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

Two spans are emitted, and no more:

* one per pipeline stage, around the whole of ``run_stage``
* one per LLM call, from a wrapper around whatever ``LLMProvider`` the factory
  built

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
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.llm.protocol import LLMProvider, Message, Usage

logger = logging.getLogger(__name__)

_TRACER: Any | None = None


def tracing_enabled() -> bool:
    return _TRACER is not None


def setup_tracing() -> bool:
    """Attach an exporter if one is configured. Returns whether tracing is on.

    Idempotent, and safe to call when the optional dependencies are absent: a
    missing package or a missing key leaves tracing off rather than failing
    startup. The service's job is to run pipelines, not to export spans.
    """
    global _TRACER
    if _TRACER is not None:
        return True

    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    if not (public_key and secret_key):
        return False

    try:
        from langfuse import get_client
        from opentelemetry import trace
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
        get_client()
        provider = trace.get_tracer_provider()
        if not isinstance(provider, TracerProvider):  # pragma: no cover - env dependent
            logger.warning("No OpenTelemetry TracerProvider is installed; tracing stays off.")
            return False
        _TRACER = trace.get_tracer("pm-intelligence-engine")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Tracing setup failed (%s); continuing without it.", exc)
        return False

    logger.info("Tracing enabled.")
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
                return await self._inner.complete(
                    messages,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    usage_sink=local,
                )
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
