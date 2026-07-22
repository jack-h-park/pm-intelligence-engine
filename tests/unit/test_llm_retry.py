import pytest

from app.llm.retry import with_retries


class Transient(Exception):
    pass


class Permanent(Exception):
    pass


def _is_transient(exc: Exception) -> bool:
    return isinstance(exc, Transient)


@pytest.mark.asyncio
async def test_returns_first_success_without_sleeping():
    async def call() -> str:
        return "ok"

    assert await with_retries(call, _is_transient, "Test", delays=[]) == "ok"


@pytest.mark.asyncio
async def test_retries_transient_then_succeeds():
    attempts = []

    async def call() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise Transient("busy")
        return "ok"

    assert await with_retries(call, _is_transient, "Test", delays=[0, 0, 0]) == "ok"
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_permanent_error_propagates_unwrapped():
    attempts = []

    async def call() -> str:
        attempts.append(1)
        raise Permanent("bad request")

    with pytest.raises(Permanent):
        await with_retries(call, _is_transient, "Test", delays=[0, 0])
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_exhausted_retries_raise_runtime_error_naming_provider():
    async def call() -> str:
        raise Transient("busy")

    with pytest.raises(RuntimeError, match="Anthropic request failed after 3 attempts"):
        await with_retries(call, _is_transient, "Anthropic", delays=[0, 0])
