"""Stage metadata names the provider and credential kind that served the stage."""

from app.models.stages import StageMetadata


def test_one_route_is_recorded():
    meta = StageMetadata.with_usage(None, [
        {"input_tokens": 10, "output_tokens": 2, "model": "m1", "provider": "openai-codex",
         "credential_kind": "oauth", "tokens_available": True},
    ])
    assert (meta.model_used, meta.provider, meta.credential_kind) == ("m1", "openai-codex", "oauth")


def test_parallel_calls_on_different_routes_are_all_recorded():
    meta = StageMetadata.with_usage(None, [
        {"input_tokens": 1, "output_tokens": 1, "model": "m1",
         "provider": "openai-codex", "credential_kind": "oauth"},
        {"input_tokens": 1, "output_tokens": 1, "model": "m1",
         "provider": "openai-codex", "credential_kind": "oauth"},
        {"input_tokens": 1, "output_tokens": 1, "model": "m1",
         "provider": "openai", "credential_kind": "api_key"},
    ])
    assert meta.provider == "openai-codex,openai"
    assert meta.credential_kind == "oauth,api_key"
    assert (meta.input_tokens, meta.output_tokens) == (3, 3)


def test_an_unmeasured_call_still_names_its_route_but_adds_no_tokens():
    meta = StageMetadata.with_usage(None, [
        {"input_tokens": 0, "output_tokens": 0, "model": "m1", "provider": "openai-codex",
         "credential_kind": "oauth", "tokens_available": False},
    ])
    assert (meta.provider, meta.credential_kind) == ("openai-codex", "oauth")
    assert (meta.input_tokens, meta.output_tokens) == (None, None)


def test_entries_without_the_fields_leave_them_empty():
    meta = StageMetadata.with_usage("fixture-model", [{"input_tokens": 4, "output_tokens": 1}])
    assert (meta.model_used, meta.provider, meta.credential_kind) == ("fixture-model", None, None)


def test_model_used_comes_from_the_reported_route_not_the_engine_default():
    meta = StageMetadata.with_usage("engine-default-model", [
        {"input_tokens": 0, "output_tokens": 0, "model": "route-model", "provider": "openai-codex",
         "credential_kind": "oauth", "tokens_available": False},
    ])
    assert meta.model_used == "route-model"
