from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.decision_case import DecisionCase
from app.services.run_context import load_run_context
from app.storage.sqlite_store import SQLiteStore


def test_store_pins_a_case_revision_to_a_run(tmp_path):
    store = SQLiteStore(f"sqlite:///{tmp_path}/workflow.db")
    signal_id = store.save_signal(
        title="Direct product decision input",
        raw_content="Should the managed policy be investigated?",
        original_product_id="android-enterprise",
    )
    run_id = store.create_run("android-enterprise", signal_id)
    case = DecisionCase(
        case_id="case-android",
        revision=1,
        prepared_context_id="prepared-android",
        prepared_context_revision=3,
        product_id="android-enterprise",
        decision_question="Should the policy be investigated?",
        input_origins=["direct"],
        authorized_depth="evaluate",
    )

    store.save_decision_case(run_id, case)

    assert store.get_decision_case(run_id) == case


def test_store_creates_direct_signal_run_and_case_link_atomically(tmp_path):
    store = SQLiteStore(f"sqlite:///{tmp_path}/workflow.db")
    case = DecisionCase(
        case_id="case-direct",
        revision=1,
        prepared_context_id="prepared-direct",
        prepared_context_revision=1,
        product_id="android-enterprise",
        decision_question="Should we inspect this policy behavior?",
        input_origins=["direct"],
        authorized_depth="evaluate",
    )

    result = store.create_decision_request_run(case)

    run = store.get_run(result["run_id"])
    signal = store.get_signal(result["signal_id"])
    assert run["product_id"] == "android-enterprise"
    assert signal["source_type"] == "manual"
    assert signal["source_ref"] == "decision-case:case-direct:1"
    assert store.get_decision_case(result["run_id"]) == case


def test_failed_duplicate_case_insert_rolls_back_the_new_signal_and_run(tmp_path):
    store = SQLiteStore(f"sqlite:///{tmp_path}/workflow.db")
    case = DecisionCase(
        case_id="case-duplicate",
        revision=1,
        prepared_context_id="prepared-direct",
        prepared_context_revision=1,
        product_id="android-enterprise",
        decision_question="Should we inspect this policy behavior?",
        input_origins=["direct"],
        authorized_depth="evaluate",
    )
    store.create_decision_request_run(case)

    with pytest.raises(IntegrityError):
        store.create_decision_request_run(case)

    assert len(store.list_signals(limit=10)) == 1
    assert len(store.list_runs(limit=10)) == 1


def test_context_reloads_the_case_pinned_to_the_run(tmp_path):
    store = SQLiteStore(f"sqlite:///{tmp_path}/workflow.db")
    case = DecisionCase(
        case_id="case-resume",
        revision=1,
        prepared_context_id="prepared-direct",
        prepared_context_revision=1,
        product_id="android-enterprise",
        decision_question="Should we inspect this policy behavior?",
        input_origins=["direct"],
        authorized_depth="evaluate",
    )
    result = store.create_decision_request_run(case)
    engine = SimpleNamespace(
        store=store,
        context_loader=SimpleNamespace(
            load_full_context=lambda _: SimpleNamespace(
                pm_identity="identity", company_context="company", product_context="product"
            )
        ),
    )

    assert load_run_context(result["run_id"], engine).decision_case == case


def test_decision_request_idempotency_returns_the_original_run(tmp_path):
    store = SQLiteStore(f"sqlite:///{tmp_path}/workflow.db")
    case = DecisionCase(
        case_id="case-request",
        revision=1,
        prepared_context_id="prepared-direct",
        prepared_context_revision=1,
        product_id="android-enterprise",
        decision_question="Should we inspect this policy behavior?",
        input_origins=["direct"],
        authorized_depth="evaluate",
    )

    first, first_status = store.create_idempotent_decision_request(
        "actor", "request-key", "request-hash", case
    )
    repeated, repeated_status = store.create_idempotent_decision_request(
        "actor", "request-key", "request-hash", case
    )

    assert first_status == 202
    assert repeated_status == 200
    assert repeated == first
    assert len(store.list_runs(limit=10)) == 1


def test_decision_request_idempotency_rejects_changed_content(tmp_path):
    store = SQLiteStore(f"sqlite:///{tmp_path}/workflow.db")
    case = DecisionCase(
        prepared_context_id="prepared-direct",
        prepared_context_revision=1,
        product_id="android-enterprise",
        decision_question="Should we inspect this policy behavior?",
        input_origins=["direct"],
        authorized_depth="evaluate",
    )
    store.create_idempotent_decision_request("actor", "request-key", "hash-a", case)

    with pytest.raises(ValueError, match="different content"):
        store.create_idempotent_decision_request("actor", "request-key", "hash-b", case)


def test_concurrent_idempotency_retries_replay_the_single_created_run(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path}/workflow.db"
    case = DecisionCase(
        case_id="case-concurrent-request",
        revision=1,
        prepared_context_id="prepared-direct",
        prepared_context_revision=1,
        product_id="android-enterprise",
        decision_question="Should we inspect this policy behavior?",
        input_origins=["direct"],
    )
    barrier = Barrier(2)
    original = SQLiteStore._create_decision_request_run
    stores = [SQLiteStore(database_url), SQLiteStore(database_url)]

    def synchronized_create(session, decision_case, pipeline_version="legacy"):
        barrier.wait(timeout=5)
        return original(session, decision_case, pipeline_version)

    monkeypatch.setattr(
        SQLiteStore, "_create_decision_request_run", staticmethod(synchronized_create)
    )

    def submit(store):
        return store.create_idempotent_decision_request(
            "actor", "concurrent-key", "request-hash", case
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(submit, store) for store in stores]
        first, second = [future.result() for future in futures]

    assert sorted(status for _, status in (first, second)) == [200, 202]
    assert first[0]["run_id"] == second[0]["run_id"]
    assert len(SQLiteStore(database_url).list_runs(limit=10)) == 1


def test_insight_backed_request_is_not_labeled_as_direct_input(tmp_path):
    store = SQLiteStore(f"sqlite:///{tmp_path}/workflow.db")
    case = DecisionCase(
        prepared_context_id="prepared-insight",
        prepared_context_revision=1,
        product_id="android-enterprise",
        decision_question="What is the lowest-risk next decision?",
        input_origins=["insight"],
        insight_references=[{"insight_id": "insight-1", "revision": 2}],
    )

    result = store.create_decision_request_run(case)

    assert store.get_signal(result["signal_id"])["title"] == "Insight-backed product decision input"
