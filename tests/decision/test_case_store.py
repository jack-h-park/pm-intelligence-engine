from app.models.decision_case import DecisionCase
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
