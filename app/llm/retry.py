"""Shared transient-failure retry for LLM providers.

Both providers wrap their `complete()` call in this so a switch of
`LLM_PROVIDER` does not also switch how resilient the engine is.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")

RETRY_DELAYS = [5, 15, 30]  # seconds between attempts


async def with_retries(
    call: Callable[[], Awaitable[T]],
    is_retryable: Callable[[Exception], bool],
    provider: str,
    delays: list[int] | None = None,
) -> T:
    delays = RETRY_DELAYS if delays is None else delays
    last_exc: Exception | None = None

    for delay in [0, *delays]:
        if delay:
            await asyncio.sleep(delay)
        try:
            return await call()
        except Exception as exc:
            if not is_retryable(exc):
                raise
            last_exc = exc

    raise RuntimeError(
        f"{provider} request failed after {len(delays) + 1} attempts: {last_exc}"
    ) from last_exc
