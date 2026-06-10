"""Unit tests for the shared LLM JSON call with bounded repair retry (US-27)."""

import json

import pytest
from unittest.mock import AsyncMock

from app.llm.json_call import MAX_REPAIR_ATTEMPTS, complete_json, parse_json

_MESSAGES = [
    {"role": "system", "content": "system text"},
    {"role": "user", "content": "user text"},
]

_VALID = {"score": 4, "key_argument": "solid", "open_question": "who? how?"}


def _llm_returning(*responses: str) -> AsyncMock:
    llm = AsyncMock()
    llm.complete = AsyncMock(side_effect=list(responses))
    return llm


def test_parse_json_plain():
    assert parse_json(json.dumps(_VALID)) == _VALID


def test_parse_json_strips_code_fences():
    wrapped = f"```json\n{json.dumps(_VALID)}\n```"
    assert parse_json(wrapped) == _VALID


@pytest.mark.asyncio
async def test_valid_first_response_makes_single_call():
    llm = _llm_returning(json.dumps(_VALID))
    data = await complete_json(llm, _MESSAGES, stage="s2", run_id="r1", max_tokens=512)
    assert data == _VALID
    assert llm.complete.call_count == 1


@pytest.mark.asyncio
async def test_malformed_then_valid_repairs_once():
    llm = _llm_returning("not json at all", json.dumps(_VALID))
    data = await complete_json(llm, _MESSAGES, stage="s2", run_id="r1")
    assert data == _VALID
    assert llm.complete.call_count == 2


@pytest.mark.asyncio
async def test_repair_prompt_includes_prior_output_and_error():
    llm = _llm_returning("{broken", json.dumps(_VALID))
    await complete_json(llm, _MESSAGES, stage="s5", run_id="r1")
    repair_messages = llm.complete.call_args_list[1].kwargs["messages"]
    # Original conversation preserved, then assistant echo, then repair instruction
    assert repair_messages[:2] == _MESSAGES
    assert repair_messages[2] == {"role": "assistant", "content": "{broken"}
    assert repair_messages[3]["role"] == "user"
    assert "not valid JSON" in repair_messages[3]["content"]


@pytest.mark.asyncio
async def test_exhausted_repairs_raise_json_error():
    bad = ["nope"] * (MAX_REPAIR_ATTEMPTS + 1)
    llm = _llm_returning(*bad)
    with pytest.raises(json.JSONDecodeError):
        await complete_json(llm, _MESSAGES, stage="s3", run_id="r1")
    # initial call + MAX_REPAIR_ATTEMPTS repair calls
    assert llm.complete.call_count == MAX_REPAIR_ATTEMPTS + 1


@pytest.mark.asyncio
async def test_llm_kwargs_forwarded_on_every_call():
    llm = _llm_returning("broken", json.dumps(_VALID))
    await complete_json(
        llm, _MESSAGES, stage="s5", run_id="r1", max_tokens=1024, temperature=0
    )
    for call in llm.complete.call_args_list:
        assert call.kwargs["max_tokens"] == 1024
        assert call.kwargs["temperature"] == 0


@pytest.mark.asyncio
async def test_repair_events_emitted(capsys):
    llm = _llm_returning("broken", json.dumps(_VALID))
    await complete_json(llm, _MESSAGES, stage="s2", run_id="r-evt")
    events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    repair_events = [e for e in events if e["action"] == "json_repair_attempt"]
    assert len(repair_events) == 1
    assert repair_events[0]["run_id"] == "r-evt"
    assert repair_events[0]["detail"]["attempt"] == 1
