"""Phase 2: per-stage / per-run LLM token usage capture.

Covers the three links of the capture chain:
  provider usage_sink → StageMetadata.with_usage → run_finalizer._sum_run_tokens
"""

import json

import pytest

from app.llm.json_call import complete_json
from app.models.stages import StageMetadata
from app.services.run_finalizer import _sum_run_tokens

_MESSAGES = [{"role": "user", "content": "x"}]


class _SinkProvider:
    """Provider that appends a fixed usage to usage_sink on each call, like the real ones."""

    def __init__(self, *responses, inp=10, out=5):
        self._responses = list(responses)
        self._i = 0
        self._inp, self._out = inp, out

    async def complete(
        self, messages, model=None, max_tokens=2048, temperature=None, usage_sink=None
    ):
        r = self._responses[self._i]
        self._i += 1
        if usage_sink is not None:
            usage_sink.append({"input_tokens": self._inp, "output_tokens": self._out})
        return r


def test_with_usage_sums_sink():
    sink = [{"input_tokens": 10, "output_tokens": 5}, {"input_tokens": 3, "output_tokens": 2}]
    m = StageMetadata.with_usage("gpt-5.4", sink)
    assert (m.model_used, m.input_tokens, m.output_tokens) == ("gpt-5.4", 13, 7)


def test_with_usage_empty_or_none_leaves_null():
    assert StageMetadata.with_usage("gpt-5.4", []).input_tokens is None
    assert StageMetadata.with_usage("gpt-5.4", None).output_tokens is None


@pytest.mark.asyncio
async def test_complete_json_accumulates_usage_across_repairs():
    # invalid → repair → valid: both underlying calls contribute to the sink
    p = _SinkProvider("not json", '{"ok": true}')
    sink: list = []
    out = await complete_json(p, _MESSAGES, stage="s2", run_id="r", usage_sink=sink)
    assert out == {"ok": True}
    assert len(sink) == 2
    assert sum(u["input_tokens"] for u in sink) == 20


@pytest.mark.asyncio
async def test_complete_json_without_sink_is_backward_compatible():
    p = _SinkProvider('{"ok": true}')
    assert await complete_json(p, _MESSAGES, stage="s2", run_id="r") == {"ok": True}


def test_sum_run_tokens_aggregates_stage_metadata():
    class _Store:
        def get_all_stage_outputs(self, run_id):
            return [
                {
                    "output_json": json.dumps(
                        {"metadata": {"input_tokens": 100, "output_tokens": 20}}
                    )
                },
                {
                    "output_json": json.dumps(
                        {"metadata": {"input_tokens": 50, "output_tokens": 10}}
                    )
                },
                {"output_json": json.dumps({"metadata": {"model_used": "x"}})},  # no tokens
                {"output_json": "not json"},  # malformed — ignored
            ]

    class _Engine:
        store = _Store()

    assert _sum_run_tokens("r", _Engine()) == {
        "prompt_tokens_total": 150,
        "completion_tokens_total": 30,
    }


def test_sum_run_tokens_empty_when_no_stage_recorded_tokens():
    class _Store:
        def get_all_stage_outputs(self, run_id):
            return [{"output_json": json.dumps({"metadata": {"model_used": "x"}})}]

    class _Engine:
        store = _Store()

    assert _sum_run_tokens("r", _Engine()) == {}
