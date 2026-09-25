import hashlib
import json

import pytest

import app.api.insights as insights_api
from app.api.deps import get_engine
from app.api.main import app
from app.insight_worker import process_backfill_one


def _seed_insight_with_evidence(engine, candidate_payload, source_payload, bundle_payload):
    from app.models.insights import InsightRevision, PreparedContext

    candidate = engine.insight_store.save_candidate(candidate_payload)
    source = engine.insight_store.save_source(
        {**source_payload, "candidate_id": candidate.candidate_id}
    )
    bundle = engine.insight_store.save_bundle(
        bundle_payload(candidate.candidate_id, source.source_id)
    )
    prepared = engine.insight_store.save_prepared_context(
        PreparedContext(
            candidate_id=candidate.candidate_id,
            bundle_id=bundle.bundle_id,
            question="What should I test?",
            constraints=["Keep claims attributed."],
            validation_status="valid",
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )
    insight = engine.insight_store.save_insight(
        InsightRevision(
            prepared_context_id=prepared.prepared_context_id,
            headline="Android control",
            explanation="A bounded change.",
            actual_change="Android added a control.",
            why_now="A release documented it.",
            personal_relevance="It informs device management.",
            takeaway="Verify it.",
            claims=[{"text": "A control was added.", "passage_ids": ["passage-fixture-1"]}],
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )
    return insight, source


def _backfill_payload(revision=1):
    return {
        "base_revision": revision,
        "targets": [
            {
                "url": "https://www.group-ib.com/blog/vwork-app-cloning-gigabud-goldfactory/",
                "purpose": "primary_incident",
                "question": "What campaign facts and malware behaviour are directly observed?",
            },
            {
                "url": "https://source.android.com/docs/devices/admin/managed-profiles",
                "purpose": "platform_behavior",
                "question": "What work-profile behaviour is documented by Android?",
            },
        ],
    }


def test_evidence_backfill_api_creates_an_idempotent_product_agnostic_request(
    client, auth_headers, candidate_payload, source_payload, bundle_payload
):
    """Catches an API path that accepts caller lineage or starts product-decision state."""
    engine = app.dependency_overrides[get_engine]()
    insight, _ = _seed_insight_with_evidence(
        engine, candidate_payload, source_payload, bundle_payload
    )
    headers = {**auth_headers, "Idempotency-Key": "gigabud-backfill-api-1"}

    first = client.post(
        f"/insights/{insight.insight_id}/evidence-backfills",
        json=_backfill_payload(insight.revision),
        headers=headers,
    )
    repeated = client.post(
        f"/insights/{insight.insight_id}/evidence-backfills",
        json=_backfill_payload(insight.revision),
        headers=headers,
    )

    assert first.status_code == 201
    assert repeated.status_code == 200
    assert repeated.json() == first.json()
    assert first.json()["candidate_id"] == engine.insight_store.get_candidate(
        engine.insight_store.get_prepared_context(insight.prepared_context_id).candidate_id
    ).candidate_id
    assert first.json()["state"] == "queued"
    assert [target["url"] for target in first.json()["targets"]] == [
        "https://www.group-ib.com/blog/vwork-app-cloning-gigabud-goldfactory",
        "https://source.android.com/docs/devices/admin/managed-profiles",
    ]
    assert [target["purpose"] for target in first.json()["targets"]] == [
        "primary_incident", "platform_behavior"
    ]
    assert not {"product_id", "review_id", "decision_case_id", "delivery_receipt_id"} & set(
        first.json()
    )


def test_insight_operation_detail_exposes_only_sanitized_status(client, auth_headers):
    engine = app.dependency_overrides[get_engine]()
    operation_id = "operation-status-fixture"
    reservation = engine.insight_store.reserve_budget(
        {
            "operation_id": operation_id,
            "operation_type": "triage",
            "policy_revision": "fixture-policy",
            "provider": "private-provider-name",
            "rate_revision": "fixture-rate",
            "maximum_micros": 100_000,
            "allowance_class": "sensing",
            "budget_window": "fixture-window",
            "state": "reserved",
        },
        allowance_micros=100_000,
    )
    assert reservation is not None
    engine.insight_store.claim_triage(operation_id)
    engine.insight_store.complete_triage(
        operation_id,
        {"decision": "keep", "source_text": "private source", "prompt": "private prompt"},
    )

    response = client.get(f"/insight-operations/{operation_id}", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {
        "operation_id": operation_id,
        "triage_state": "complete",
        "has_triage_result": True,
        "reservation": {
            "reservation_id": reservation.reservation_id,
            "state": "reserved",
            "allowance_class": "sensing",
            "maximum_micros": 100_000,
            "actual_micros": None,
        },
    }
    assert "private source" not in response.text
    assert "private prompt" not in response.text
    assert "private-provider-name" not in response.text


def test_insight_operation_detail_returns_not_found_for_unknown_operation(client, auth_headers):
    response = client.get("/insight-operations/not-recorded", headers=auth_headers)

    assert response.status_code == 404


def test_insight_operation_detail_requires_bearer_authentication(client):
    response = client.get("/insight-operations/not-recorded")

    assert response.status_code == 401


def test_insight_operation_detail_reports_reservation_without_triage(client, auth_headers):
    engine = app.dependency_overrides[get_engine]()
    operation_id = "reservation-only-fixture"
    reservation = engine.insight_store.reserve_budget(
        {
            "operation_id": operation_id,
            "operation_type": "triage",
            "policy_revision": "fixture-policy",
            "provider": "private-provider-name",
            "rate_revision": "fixture-rate",
            "maximum_micros": 50_000,
            "allowance_class": "sensing",
            "budget_window": "fixture-window",
            "state": "reserved",
        },
        allowance_micros=50_000,
    )
    assert reservation is not None

    response = client.get(f"/insight-operations/{operation_id}", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {
        "operation_id": operation_id,
        "triage_state": None,
        "has_triage_result": False,
        "reservation": {
            "reservation_id": reservation.reservation_id,
            "state": "reserved",
            "allowance_class": "sensing",
            "maximum_micros": 50_000,
            "actual_micros": None,
        },
    }


def test_evidence_backfill_api_rejects_changed_replays_and_invalid_bases(
    client, auth_headers, candidate_payload, source_payload, bundle_payload
):
    """Catches a retry widening targets or a request attached to a missing/stale Insight."""
    engine = app.dependency_overrides[get_engine]()
    insight, _ = _seed_insight_with_evidence(
        engine, candidate_payload, source_payload, bundle_payload
    )
    headers = {**auth_headers, "Idempotency-Key": "gigabud-backfill-api-conflict"}
    assert client.post(
        f"/insights/{insight.insight_id}/evidence-backfills",
        json=_backfill_payload(insight.revision),
        headers=headers,
    ).status_code == 201

    changed = client.post(
        f"/insights/{insight.insight_id}/evidence-backfills",
        json={
            "base_revision": insight.revision,
            "targets": [{
                "url": "https://example.test/unapproved-expansion",
                "purpose": "independent_corroboration",
                "question": "What independently corroborates the campaign?",
            }],
        },
        headers=headers,
    )
    widened = client.post(
        f"/insights/{insight.insight_id}/evidence-backfills",
        json={
            **_backfill_payload(insight.revision),
            "targets": [
                *_backfill_payload(insight.revision)["targets"],
                {
                    "url": "https://example.test/unapproved-third-source",
                    "purpose": "independent_corroboration",
                    "question": "What new source should be added?",
                },
            ],
        },
        headers=headers,
    )
    missing = client.post(
        "/insights/missing-insight/evidence-backfills",
        json=_backfill_payload(),
        headers={**auth_headers, "Idempotency-Key": "backfill-missing"},
    )
    stale = client.post(
        f"/insights/{insight.insight_id}/evidence-backfills",
        json=_backfill_payload(insight.revision + 1),
        headers={**auth_headers, "Idempotency-Key": "backfill-stale"},
    )
    caller_candidate = client.post(
        f"/insights/{insight.insight_id}/evidence-backfills",
        json={**_backfill_payload(insight.revision), "candidate_id": "caller-selected"},
        headers={**auth_headers, "Idempotency-Key": "backfill-caller-candidate"},
    )
    unsafe_target = client.post(
        f"/insights/{insight.insight_id}/evidence-backfills",
        json={
            "base_revision": insight.revision,
            "targets": [{
                "url": "file:///private/source",
                "purpose": "primary_incident",
                "question": "What is directly observed?",
            }],
        },
        headers={**auth_headers, "Idempotency-Key": "backfill-unsafe-target"},
    )

    assert changed.status_code == 409
    assert widened.status_code == 409
    assert missing.status_code == 404
    assert stale.status_code == 404
    assert caller_candidate.status_code == 422
    assert unsafe_target.status_code == 422


def test_evidence_backfill_worker_tick_advances_only_the_named_backfill(
    client, auth_headers, candidate_payload, source_payload, bundle_payload, monkeypatch
):
    """Catches a control-plane tick falling through to the generic worker queue."""
    engine = app.dependency_overrides[get_engine]()
    insight, _ = _seed_insight_with_evidence(
        engine, candidate_payload, source_payload, bundle_payload
    )
    created = client.post(
        f"/insights/{insight.insight_id}/evidence-backfills",
        json=_backfill_payload(insight.revision),
        headers={**auth_headers, "Idempotency-Key": "scoped-backfill-endpoint"},
    )
    backfill_id = created.json()["backfill_id"]
    unrelated, _ = engine.insight_store.create_idempotent_job(
        "fixture-reviewer",
        "unrelated-learning-job",
        "unrelated-learning-job-hash",
        {
            "candidate_id": created.json()["candidate_id"],
            "context_revision": "fixture-v1",
            "purpose": "learning",
        },
    )

    async def tick(store, requested_backfill_id):
        return await process_backfill_one(store, requested_backfill_id, object())

    monkeypatch.setattr(insights_api, "run_oauth_backfill_worker_tick", tick)
    response = client.post(f"/evidence-backfills/{backfill_id}/worker-tick", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["state"] == "waiting_research"
    assert engine.insight_store.get_job(unrelated.job_id).state == "queued"


def test_scoped_candidate_worker_tick_never_falls_through_to_generic_work(
    client, auth_headers, candidate_payload, source_payload, monkeypatch
):
    """The Candidate endpoint may initialize only its named, marked job."""
    engine = app.dependency_overrides[get_engine]()
    candidate = engine.insight_store.save_candidate(candidate_payload)
    engine.insight_store.save_source({**source_payload, "candidate_id": candidate.candidate_id})
    unrelated = engine.insight_store.create_job(
        {
            "candidate_id": candidate.candidate_id,
            "context_revision": "generic-fixture-v1",
            "purpose": "learning",
        }
    )

    async def tick(store, requested_candidate_id):
        store.ensure_scoped_candidate_job(requested_candidate_id)
        return None

    monkeypatch.setattr(insights_api, "run_oauth_scoped_candidate_worker_tick", tick)
    response = client.post(
        f"/insight-candidates/{candidate.candidate_id}/worker-tick", headers=auth_headers
    )

    assert response.status_code == 200
    assert response.json()["candidate_id"] == candidate.candidate_id
    assert response.json()["state"] == "queued"
    assert response.json()["completion_disposition"] is None
    assert engine.insight_store.get_job(unrelated.job_id).state == "queued"


def test_scoped_candidate_worker_tick_replays_its_completed_insight_id(
    client, auth_headers, candidate_payload, source_payload, monkeypatch
):
    """A transport retry must retain the reviewable Insight identifier."""
    from app.models.insights import InsightRevision, PreparedContext

    engine = app.dependency_overrides[get_engine]()
    store = engine.insight_store
    candidate = store.save_candidate(candidate_payload)
    source = store.save_source({**source_payload, "candidate_id": candidate.candidate_id})
    scoped = store.ensure_scoped_candidate_job(candidate.candidate_id)
    claimed = store.claim_scoped_candidate_job(candidate.candidate_id)
    prepared = PreparedContext(
        candidate_id=candidate.candidate_id,
        bundle_id=scoped.bundle_id,
        question="What changed?",
        validation_status="valid",
        context_revision=scoped.context_revision,
    )
    insight = InsightRevision(
        prepared_context_id=prepared.prepared_context_id,
        headline="Scoped learning",
        explanation="Only the stored source was used.",
        actual_change="A bounded source was reviewed.",
        why_now="The explicit Candidate was requested.",
        personal_relevance="It answers the requested question.",
        takeaway="Review the evidence.",
        claims=[
            {"text": "A bounded source was reviewed.", "passage_ids": [f"{source.source_id}:0"]}
        ],
        context_revision=scoped.context_revision,
    )
    store.complete_job_analysis(claimed.job_id, claimed.lease_token, prepared, insight)

    async def tick(store, requested_candidate_id):
        return None

    monkeypatch.setattr(insights_api, "run_oauth_scoped_candidate_worker_tick", tick)
    response = client.post(
        f"/insight-candidates/{candidate.candidate_id}/worker-tick", headers=auth_headers
    )

    assert response.status_code == 200
    assert response.json()["state"] == "complete"
    assert response.json()["insight_id"] == insight.insight_id


def test_reused_key_with_changed_body_conflicts(client, auth_headers, candidate_payload):
    headers = {**auth_headers, "Idempotency-Key": "candidate-fixture-1"}
    first = client.post("/insight-candidates", json=candidate_payload, headers=headers)
    repeated = client.post("/insight-candidates", json=candidate_payload, headers=headers)
    changed = client.post(
        "/insight-candidates",
        json={**candidate_payload, "subject": "Different"},
        headers=headers,
    )

    assert first.status_code == 201
    assert repeated.status_code == 200
    assert repeated.json()["candidate_id"] == first.json()["candidate_id"]
    assert changed.status_code == 409


def test_migration_import_requires_the_saved_inventory_hash(
    client, auth_headers, candidate_payload
):
    candidate = client.post(
        "/insight-candidates",
        json=candidate_payload,
        headers={**auth_headers, "Idempotency-Key": "candidate-key"},
    )
    assert candidate.status_code == 201

    inventory = client.post("/insight-migration-inventories", headers=auth_headers)
    assert inventory.status_code == 201
    body = inventory.json()

    rejected = client.post(
        f"/insight-migration-inventories/{body['inventory_id']}/import",
        json={"inventory_hash": "0" * 64, "batch_size": 100},
        headers=auth_headers,
    )
    accepted = client.post(
        f"/insight-migration-inventories/{body['inventory_id']}/import",
        json={"inventory_hash": body["inventory_hash"], "batch_size": 100},
        headers=auth_headers,
    )

    assert rejected.status_code == 409
    assert accepted.status_code == 202
    assert accepted.json() == {
        "inventory_id": body["inventory_id"],
        "imported_count": 1,
        "complete": True,
    }


def test_missing_inventory_is_a_404_not_a_conflict(client, auth_headers):
    response = client.post(
        "/insight-migration-inventories/inventory-absent/import",
        json={"inventory_hash": "0" * 64},
        headers=auth_headers,
    )

    assert response.status_code == 404


def test_saved_inventory_is_readable_back(client, auth_headers, candidate_payload):
    client.post(
        "/insight-candidates",
        json=candidate_payload,
        headers={**auth_headers, "Idempotency-Key": "candidate-key"},
    )
    created = client.post("/insight-migration-inventories", headers=auth_headers).json()

    fetched = client.get(
        f"/insight-migration-inventories/{created['inventory_id']}", headers=auth_headers
    )

    assert fetched.status_code == 200
    assert fetched.json() == created


def test_migration_overlay_requires_release_flag(
    client, auth_headers, candidate_payload, monkeypatch
):
    from config import settings

    candidate = client.post(
        "/insight-candidates",
        json=candidate_payload,
        headers={**auth_headers, "Idempotency-Key": "candidate-key"},
    )
    assert candidate.status_code == 201
    inventory = client.post("/insight-migration-inventories", headers=auth_headers).json()
    imported = client.post(
        f"/insight-migration-inventories/{inventory['inventory_id']}/import",
        json={"inventory_hash": inventory["inventory_hash"]},
        headers=auth_headers,
    )
    assert imported.status_code == 202

    blocked = client.post(
        f"/insight-migration-inventories/{inventory['inventory_id']}/overlay",
        json={"inventory_hash": inventory["inventory_hash"], "enabled": True},
        headers=auth_headers,
    )
    monkeypatch.setattr(settings, "INSIGHT_MIGRATION_ACTIVATION_ENABLED", True)
    enabled = client.post(
        f"/insight-migration-inventories/{inventory['inventory_id']}/overlay",
        json={"inventory_hash": inventory["inventory_hash"], "enabled": True},
        headers=auth_headers,
    )
    disabled = client.post(
        f"/insight-migration-inventories/{inventory['inventory_id']}/overlay",
        json={"inventory_hash": inventory["inventory_hash"], "enabled": False},
        headers=auth_headers,
    )

    assert blocked.status_code == 409
    assert enabled.json() == {"inventory_id": inventory["inventory_id"], "enabled": True}
    assert disabled.json() == {"inventory_id": inventory["inventory_id"], "enabled": False}


def test_intake_routes_require_bearer_authentication(client, candidate_payload):
    response = client.post(
        "/insight-candidates",
        json=candidate_payload,
        headers={"Idempotency-Key": "unauthenticated"},
    )

    assert response.status_code == 401


def test_intake_routes_require_an_idempotency_key(client, auth_headers, candidate_payload):
    response = client.post("/insight-candidates", json=candidate_payload, headers=auth_headers)

    assert response.status_code == 422


def test_source_and_bundle_intake_validate_references(
    client, auth_headers, candidate_payload, source_payload, bundle_payload
):
    candidate = client.post(
        "/insight-candidates",
        json=candidate_payload,
        headers={**auth_headers, "Idempotency-Key": "candidate-for-source"},
    )
    assert candidate.status_code == 201

    bad_source = client.post(
        "/insight-sources",
        json={**source_payload, "candidate_id": "missing-candidate"},
        headers={**auth_headers, "Idempotency-Key": "bad-source"},
    )
    assert bad_source.status_code == 404

    source = client.post(
        "/insight-sources",
        json={**source_payload, "candidate_id": candidate.json()["candidate_id"]},
        headers={**auth_headers, "Idempotency-Key": "valid-source"},
    )
    assert source.status_code == 201

    bad_bundle = client.post(
        "/insight-evidence",
        json=bundle_payload(candidate.json()["candidate_id"], "not-a-source"),
        headers={**auth_headers, "Idempotency-Key": "bad-bundle"},
    )
    assert bad_bundle.status_code == 422


def test_identical_source_submission_returns_the_existing_source(
    client, auth_headers, candidate_payload, source_payload
):
    candidate = client.post(
        "/insight-candidates",
        json=candidate_payload,
        headers={**auth_headers, "Idempotency-Key": "candidate-for-dedupe"},
    ).json()
    content = source_payload["content"]
    payload = {
        **source_payload,
        "candidate_id": candidate["candidate_id"],
        "content_hash": hashlib.sha256(content.encode()).hexdigest(),
    }

    first = client.post(
        "/insight-sources", json=payload, headers={**auth_headers, "Idempotency-Key": "source-1"}
    )
    second = client.post(
        "/insight-sources", json=payload, headers={**auth_headers, "Idempotency-Key": "source-2"}
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["source_id"] == first.json()["source_id"]


def test_novelty_lookup_returns_only_known_source_hashes(
    client, auth_headers, candidate_payload, source_payload
):
    candidate = client.post(
        "/insight-candidates",
        json=candidate_payload,
        headers={**auth_headers, "Idempotency-Key": "candidate-for-novelty"},
    ).json()
    client.post(
        "/insight-sources",
        json={**source_payload, "candidate_id": candidate["candidate_id"]},
        headers={**auth_headers, "Idempotency-Key": "source-for-novelty"},
    )

    response = client.post(
        "/insight-triage/novelty",
        json={"content_hashes": [source_payload["content_hash"], "b" * 64]},
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json() == {"known_content_hashes": [source_payload["content_hash"]]}


def test_semantic_triage_reserves_before_calling_the_model(client, auth_headers, monkeypatch):
    from config import settings

    class FixtureLLM:
        calls = 0

        async def complete(self, messages, **kwargs):
            self.calls += 1
            return json.dumps({
                "disposition": "admit", "relevance": "relevant",
                "novelty": "meaningful_delta", "reason": "New evidence.",
            })

    engine = app.dependency_overrides[get_engine]()
    engine.llm = FixtureLLM()
    monkeypatch.setattr(settings, "INTELLIGENCE_SENSING_ALLOWANCE_MICROS", 10)
    monkeypatch.setattr(settings, "INTELLIGENCE_RATE_REVISION", "fixture-rates")
    response = client.post(
        "/insight-triage",
        json={
            "question": "What changed?", "title": "Change", "content": "Evidence.",
            "operation_id": "triage-api", "policy_revision": "fixture-v1", "provider": "fixture",
            "rate_revision": "fixture-rates", "maximum_micros": 10, "actual_micros": 4,
        },
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["disposition"] == "admit"
    assert engine.insight_store.operational_summary()["cost_micros"]["finalized"] == 4
    assert engine.llm.calls == 1

    repeated = client.post(
        "/insight-triage",
        json={
            "question": "What changed?", "title": "Change", "content": "Evidence.",
            "operation_id": "triage-api", "policy_revision": "fixture-v1", "provider": "fixture",
            "rate_revision": "fixture-rates", "maximum_micros": 10, "actual_micros": 4,
        },
        headers=auth_headers,
    )
    assert repeated.json() == response.json()
    assert engine.llm.calls == 1


def _write_interest_triage_context(tmp_path):
    context = tmp_path / "decision-context" / "core"
    context.mkdir(exist_ok=True)
    (context / "signal-interest-context.yaml").write_text(
        "revision: fixture-v1\n"
        "interests:\n"
        "  - id: learning-loop\n"
        "    question: What should I test?\n"
        "    constraints:\n"
        "      - Keep claims attributed.\n",
        encoding="utf-8",
    )


def _interest_triage_payload(interest_id: str, operation_id: str = "interest-triage"):
    return {
        "interest_id": interest_id,
        "title": "Change",
        "content": "Evidence.",
        "operation_id": operation_id,
        "policy_revision": "fixture-v1",
        "provider": "oauth",
        "rate_revision": "fixture-rates",
        "maximum_micros": 10,
        "actual_micros": 1,
    }


def test_interest_triage_resolves_question_inside_engine(
    client, auth_headers, monkeypatch, tmp_path
):
    _write_interest_triage_context(tmp_path)
    captured = {}
    engine = app.dependency_overrides[get_engine]()
    product_decision_llm = object()
    s2k_llm = object()
    engine.llm = product_decision_llm
    monkeypatch.setattr(insights_api, "build_s2k_llm_provider", lambda: s2k_llm)
    triage_calls = 0

    async def capture_triage(**kwargs):
        nonlocal triage_calls
        triage_calls += 1
        captured.update(kwargs)
        return insights_api.TriageDecision(
            disposition="admit",
            relevance="relevant",
            novelty="meaningful_delta",
            reason="New evidence.",
        )

    monkeypatch.setattr(insights_api, "triage_with_reservation", capture_triage)
    monkeypatch.setattr(insights_api, "utc_day_window", lambda: "2026-09-21")

    payload = _interest_triage_payload("learning-loop")
    response = client.post(
        "/insight-triage/interest",
        json=payload,
        headers=auth_headers,
    )
    monkeypatch.setattr(
        insights_api,
        "build_s2k_llm_provider",
        lambda: (_ for _ in ()).throw(ValueError("bridge unavailable after first call")),
    )
    repeated = client.post("/insight-triage/interest", json=payload, headers=auth_headers)

    assert response.status_code == 200
    assert repeated.json() == response.json()
    assert captured["question"] == "What should I test?"
    assert captured["reservation_payload"]["budget_window"] == "2026-09-21"
    assert captured["llm"] is s2k_llm
    assert engine.llm is product_decision_llm
    assert triage_calls == 1


def test_interest_triage_transport_failure_keeps_unknown_reservation_and_prevents_duplicate_call(
    client, auth_headers, monkeypatch, tmp_path
):
    from config import settings

    _write_interest_triage_context(tmp_path)
    monkeypatch.setattr(settings, "INTELLIGENCE_SENSING_ALLOWANCE_MICROS", 10)
    monkeypatch.setattr(settings, "INTELLIGENCE_RATE_REVISION", "fixture-rates")
    engine = app.dependency_overrides[get_engine]()

    class BrokenLLM:
        calls = 0

        async def complete(self, messages, **_kwargs):
            self.calls += 1
            raise RuntimeError("fixture transport failure")

    provider = BrokenLLM()
    monkeypatch.setattr(insights_api, "build_s2k_llm_provider", lambda: provider)
    payload = _interest_triage_payload("learning-loop", "transport-failed-once")

    with pytest.raises(RuntimeError, match="fixture transport failure"):
        client.post("/insight-triage/interest", json=payload, headers=auth_headers)
    first_summary = engine.insight_store.operational_summary()
    assert first_summary["cost_micros"] == {"reserved": 10, "finalized": 0, "unknown": 10}
    assert first_summary["candidates"] == 0
    assert engine.insight_store.list_insights() == []

    repeated = client.post("/insight-triage/interest", json=payload, headers=auth_headers)

    assert repeated.status_code == 409
    assert repeated.json()["detail"] == "triage_in_progress"
    assert provider.calls == 1
    assert engine.insight_store.operational_summary()["cost_micros"] == first_summary["cost_micros"]


def test_interest_triage_rejects_unknown_id_before_model(
    client, auth_headers, monkeypatch, tmp_path
):
    _write_interest_triage_context(tmp_path)

    def provider_must_not_be_built():
        raise AssertionError("S2K provider must not build for an unknown interest")

    monkeypatch.setattr(insights_api, "build_s2k_llm_provider", provider_must_not_be_built)

    async def must_not_run(**kwargs):
        raise AssertionError("semantic triage must not run for an unknown interest")

    monkeypatch.setattr(insights_api, "triage_with_reservation", must_not_run)

    response = client.post(
        "/insight-triage/interest",
        json=_interest_triage_payload("unknown"),
        headers=auth_headers,
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "unknown_interest_id"


def test_job_intake_is_idempotent_and_requires_an_existing_candidate(
    client, auth_headers, candidate_payload
):
    candidate = client.post(
        "/insight-candidates",
        json=candidate_payload,
        headers={**auth_headers, "Idempotency-Key": "candidate-for-job"},
    ).json()
    payload = {
        "candidate_id": candidate["candidate_id"],
        "context_revision": "fixture-context-v1",
        "purpose": "learning",
        "bundle_id": None,
    }
    headers = {**auth_headers, "Idempotency-Key": "job-fixture-1"}

    first = client.post("/insight-jobs", json=payload, headers=headers)
    repeated = client.post("/insight-jobs", json=payload, headers=headers)
    detail = client.get(f"/insight-jobs/{first.json()['job_id']}", headers=auth_headers)

    assert first.status_code == 202
    assert repeated.status_code == 200
    assert repeated.json()["job_id"] == first.json()["job_id"]
    assert detail.json()["state"] == "queued"


def test_budget_denial_never_creates_a_paid_reservation(client, auth_headers):
    response = client.post(
        "/insight-budget/reservations",
        json={
            "operation_id": "fixture-paid-operation",
            "operation_type": "analysis",
            "policy_revision": "fixture-policy-v1",
            "provider": "fixture-provider",
            "rate_revision": "fixture-rates-v1",
            "maximum_micros": 1,
            "allowance_class": "sensing",
        },
        headers={**auth_headers, "Idempotency-Key": "budget-denied"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "budget_denied"


def test_insight_evidence_response_shape_includes_stored_context_and_excludes_uncited_passages(
    client, auth_headers, candidate_payload, source_payload, bundle_payload
):
    def bundle_with_uncited_passage(candidate_id, source_id):
        payload = bundle_payload(candidate_id, source_id)
        payload["passages"].append(
            {
                "passage_id": "passage-fixture-uncited",
                "source_id": source_id,
                "locator": "uncited statement",
                "text": "This uncited passage must remain private.",
                "role": "enrichment",
            }
        )
        return payload

    engine = app.dependency_overrides[get_engine]()
    insight, source = _seed_insight_with_evidence(
        engine, candidate_payload, source_payload, bundle_with_uncited_passage
    )

    response = client.get(
        f"/insights/{insight.insight_id}/evidence?revision=1", headers=auth_headers
    )

    assert response.status_code == 200
    assert response.json() == {
        "insight_id": insight.insight_id,
        "revision": 1,
        "question": "What should I test?",
        "constraints": ["Keep claims attributed."],
        "sources": [
            {
                "source_id": source.source_id,
                "origin": "user_supplied",
                "acquisition_status": "ok",
                "retrieved_at": "2026-09-08T00:00:00Z",
                "url": None,
                "legacy_reference": None,
            }
        ],
        "passages": [
            {
                "passage_id": "passage-fixture-1",
                "source_id": source.source_id,
                "locator": "user statement",
                "text": "The user observed selected intent sharing in a managed work profile.",
                "role": "seed",
            }
        ],
        "claim_passage_links": [{"claim_index": 0, "passage_ids": ["passage-fixture-1"]}],
        "coarse_evidence": True,
    }


def test_insight_evidence_rejects_a_stale_revision(
    client, auth_headers, candidate_payload, source_payload, bundle_payload
):
    engine = app.dependency_overrides[get_engine]()
    insight, _ = _seed_insight_with_evidence(
        engine, candidate_payload, source_payload, bundle_payload
    )

    response = client.get(
        f"/insights/{insight.insight_id}/evidence?revision=2", headers=auth_headers
    )

    assert response.status_code == 409


def test_list_insights_since_excludes_an_older_insight(
    client, auth_headers, candidate_payload, source_payload, bundle_payload
):
    engine = app.dependency_overrides[get_engine]()
    insight, _ = _seed_insight_with_evidence(
        engine, candidate_payload, source_payload, bundle_payload
    )

    response = client.get(
        "/insights?since=2999-01-01T00:00:00Z", headers=auth_headers
    )

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}
    assert insight.insight_id not in {item["insight_id"] for item in response.json()["items"]}


def test_list_insights_cursor_pages_without_a_second_consumer_ledger(
    client, auth_headers, candidate_payload, source_payload, bundle_payload
):
    engine = app.dependency_overrides[get_engine]()
    first, _ = _seed_insight_with_evidence(
        engine, candidate_payload, source_payload, bundle_payload
    )
    changed_source = "A distinct second source fixture."
    second, _ = _seed_insight_with_evidence(
        engine,
        {**candidate_payload, "subject": "Second incremental fixture"},
        {
            **source_payload,
            "content": changed_source,
            "content_hash": hashlib.sha256(changed_source.encode()).hexdigest(),
        },
        bundle_payload,
    )

    first_page = client.get("/insights?limit=1", headers=auth_headers)
    assert first_page.status_code == 200
    cursor = first_page.json()["next_cursor"]
    assert cursor is not None

    second_page = client.get(f"/insights?after={cursor}&limit=1", headers=auth_headers)
    assert second_page.status_code == 200
    terminal_cursor = second_page.json()["next_cursor"]
    assert terminal_cursor is not None
    empty_page = client.get(f"/insights?after={terminal_cursor}&limit=1", headers=auth_headers)
    assert empty_page.status_code == 200
    assert empty_page.json() == {"items": [], "next_cursor": None}
    returned_ids = {
        item["insight_id"]
        for item in first_page.json()["items"] + second_page.json()["items"]
    }
    assert returned_ids == {
        first.insight_id,
        second.insight_id,
    }
    for item in first_page.json()["items"] + second_page.json()["items"]:
        assert "knowledge_verdict" in item
        assert item["knowledge_verdict"] is None


def test_list_insights_cursor_keeps_its_since_boundary_when_omitted_on_next_page(
    client, auth_headers, candidate_payload, source_payload, bundle_payload
):
    engine = app.dependency_overrides[get_engine]()
    first, _ = _seed_insight_with_evidence(
        engine, candidate_payload, source_payload, bundle_payload
    )
    changed_source = "A distinct since cursor fixture."
    second, _ = _seed_insight_with_evidence(
        engine,
        {**candidate_payload, "subject": "Second since cursor fixture"},
        {
            **source_payload,
            "content": changed_source,
            "content_hash": hashlib.sha256(changed_source.encode()).hexdigest(),
        },
        bundle_payload,
    )

    first_page = client.get(
        "/insights?since=2020-01-01T00:00:00Z&limit=1", headers=auth_headers
    )
    cursor = first_page.json()["next_cursor"]
    second_page = client.get(f"/insights?after={cursor}&limit=1", headers=auth_headers)

    assert second_page.status_code == 200
    returned_ids = {
        item["insight_id"]
        for item in first_page.json()["items"] + second_page.json()["items"]
    }
    assert returned_ids == {
        first.insight_id,
        second.insight_id,
    }
    for item in first_page.json()["items"] + second_page.json()["items"]:
        assert "knowledge_verdict" in item
        assert item["knowledge_verdict"] is None


def test_list_insights_cursor_rejects_a_different_since_boundary(
    client, auth_headers, candidate_payload, source_payload, bundle_payload
):
    engine = app.dependency_overrides[get_engine]()
    _seed_insight_with_evidence(engine, candidate_payload, source_payload, bundle_payload)
    changed_source = "A distinct cursor-boundary fixture."
    _seed_insight_with_evidence(
        engine,
        {**candidate_payload, "subject": "Second cursor boundary fixture"},
        {
            **source_payload,
            "content": changed_source,
            "content_hash": hashlib.sha256(changed_source.encode()).hexdigest(),
        },
        bundle_payload,
    )

    first_page = client.get("/insights?limit=1", headers=auth_headers)
    cursor = first_page.json()["next_cursor"]
    response = client.get(
        f"/insights?since=2026-09-16T00:00:00Z&after={cursor}", headers=auth_headers
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "cursor since boundary does not match request"


def test_authenticated_insight_search_returns_stored_revision(
    client, auth_headers, candidate_payload, source_payload, bundle_payload, monkeypatch
):
    from app.models.insights import InsightRevision, PreparedContext
    from config import settings

    engine = app.dependency_overrides[get_engine]()
    candidate = engine.insight_store.save_candidate(candidate_payload)
    source = engine.insight_store.save_source(
        {**source_payload, "candidate_id": candidate.candidate_id}
    )
    bundle = engine.insight_store.save_bundle(
        bundle_payload(candidate.candidate_id, source.source_id)
    )
    prepared = engine.insight_store.save_prepared_context(
        PreparedContext(
            candidate_id=candidate.candidate_id,
            bundle_id=bundle.bundle_id,
            question="What changed?",
            validation_status="valid",
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )
    insight = engine.insight_store.save_insight(
        InsightRevision(
            prepared_context_id=prepared.prepared_context_id,
            headline="Android control",
            explanation="A bounded change.",
            actual_change="Android added a control.",
            why_now="A release documented it.",
            personal_relevance="It informs device management.",
            takeaway="Verify it.",
            claims=[{"text": "A control was added.", "passage_ids": ["passage-fixture-1"]}],
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )

    response = client.get("/insights/search?q=Android", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["items"][0]["insight_id"] == insight.insight_id

    detail = client.get(f"/insights/{insight.insight_id}", headers=auth_headers)
    assert detail.status_code == 200
    assert detail.json()["takeaway"] == "Verify it."

    operations = client.get("/insight-operations", headers=auth_headers)
    assert operations.status_code == 200
    assert operations.json()["candidates"] == 1
    assert operations.json()["cost_micros"] == {
        "reserved": 0, "finalized": 0, "unknown": 0,
    }
    assert operations.json()["feedback"] == {"recorded": 0, "unknown": 0}

    suppressed = client.post(
        f"/insights/{insight.insight_id}/delivery-receipts",
        json={"revision": insight.revision, "channel": "telegram", "state": "queued"},
        headers=auth_headers,
    )
    assert suppressed.status_code == 409
    monkeypatch.setattr(settings, "INTELLIGENCE_MODE", "insights")
    receipt = client.post(
        f"/insights/{insight.insight_id}/delivery-receipts",
        json={"revision": insight.revision, "channel": "telegram", "state": "queued"},
        headers=auth_headers,
    )
    repeated = client.post(
        f"/insights/{insight.insight_id}/delivery-receipts",
        json={"revision": insight.revision, "channel": "telegram", "state": "queued"},
        headers=auth_headers,
    )
    assert receipt.status_code == 201
    assert repeated.json() == receipt.json()

    feedback = client.post(
        f"/insights/{insight.insight_id}/feedback",
        json={"revision": insight.revision, "label": "useful"},
        headers=auth_headers,
    )
    assert feedback.status_code == 200
    assert feedback.json()["label"] == "useful"
    # Silence is not invented as a negative or positive feedback record.
    assert client.get("/insight-operations", headers=auth_headers).json()["feedback"] == {
        "recorded": 1, "unknown": 0,
    }


def test_authenticated_insight_listing_returns_product_agnostic_revision(
    client, auth_headers, candidate_payload, source_payload, bundle_payload
):
    from app.models.insights import InsightRevision, PreparedContext

    engine = app.dependency_overrides[get_engine]()
    candidate = engine.insight_store.save_candidate(candidate_payload)
    source = engine.insight_store.save_source(
        {**source_payload, "candidate_id": candidate.candidate_id}
    )
    bundle = engine.insight_store.save_bundle(
        bundle_payload(candidate.candidate_id, source.source_id)
    )
    prepared = engine.insight_store.save_prepared_context(
        PreparedContext(
            candidate_id=candidate.candidate_id,
            bundle_id=bundle.bundle_id,
            question="What changed?",
            validation_status="valid",
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )
    insight = engine.insight_store.save_insight(
        InsightRevision(
            prepared_context_id=prepared.prepared_context_id,
            headline="Android work-profile learning",
            explanation="A bounded learning observation.",
            actual_change="A work profile changed the observed boundary.",
            why_now="New evidence is available.",
            personal_relevance="It informs enterprise mobile security.",
            takeaway="Review profile-boundary controls.",
            claims=[{"text": "The boundary changed.", "passage_ids": ["passage-fixture-1"]}],
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )

    response = client.get("/insights?limit=10", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["items"][0]["insight_id"] == insight.insight_id
    assert "product_id" not in response.json()["items"][0]


def test_insight_review_is_authenticated_idempotent_and_does_not_start_a_decision(
    client, auth_headers, candidate_payload, source_payload, bundle_payload
):
    from app.models.insights import InsightRevision, PreparedContext

    engine = app.dependency_overrides[get_engine]()
    candidate = engine.insight_store.save_candidate(candidate_payload)
    source = engine.insight_store.save_source(
        {**source_payload, "candidate_id": candidate.candidate_id}
    )
    bundle = engine.insight_store.save_bundle(
        bundle_payload(candidate.candidate_id, source.source_id)
    )
    prepared = engine.insight_store.save_prepared_context(
        PreparedContext(
            candidate_id=candidate.candidate_id,
            bundle_id=bundle.bundle_id,
            question="What changed?",
            validation_status="valid",
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )
    insight = engine.insight_store.save_insight(
        InsightRevision(
            prepared_context_id=prepared.prepared_context_id,
            headline="Android work-profile learning",
            explanation="A bounded learning observation.",
            actual_change="A work profile changed the observed boundary.",
            why_now="New evidence is available.",
            personal_relevance="It informs enterprise mobile security.",
            takeaway="Review profile-boundary controls.",
            claims=[{"text": "The boundary changed.", "passage_ids": ["passage-fixture-1"]}],
            context_revision="fixture-v1",
        ).model_dump(mode="json")
    )
    review_payload = {
        "revision": insight.revision,
        "disposition": "retain",
        "note": "Useful boundary to remember.",
    }
    headers = {**auth_headers, "Idempotency-Key": "insight-review-1"}

    first = client.post(
        f"/insights/{insight.insight_id}/reviews", json=review_payload, headers=headers
    )
    repeated = client.post(
        f"/insights/{insight.insight_id}/reviews", json=review_payload, headers=headers
    )
    listed = client.get(f"/insights/{insight.insight_id}/reviews", headers=auth_headers)

    assert engine.store is None
    assert first.status_code == 201
    assert repeated.status_code == 200
    assert repeated.json() == first.json()
    assert listed.status_code == 200
    assert listed.json()["items"] == [first.json()]
    assert "product_id" not in first.json()
    assert "insight-test-token" not in first.text

    unauthenticated = client.post(
        f"/insights/{insight.insight_id}/reviews",
        json=review_payload,
        headers={"Idempotency-Key": "review-no-auth"},
    )
    stale = client.post(
        f"/insights/{insight.insight_id}/reviews",
        json={**review_payload, "revision": insight.revision + 1},
        headers={**auth_headers, "Idempotency-Key": "review-stale"},
    )
    product_attempt = client.post(
        f"/insights/{insight.insight_id}/reviews",
        json={**review_payload, "product_id": "samsung-knox-mtd"},
        headers={**auth_headers, "Idempotency-Key": "review-product-attempt"},
    )

    assert unauthenticated.status_code == 401
    assert stale.status_code == 409
    assert product_attempt.status_code == 422
