import hashlib
import json

from app.api.deps import get_engine
from app.api.main import app


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
        async def complete(self, messages, **kwargs):
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


def test_authenticated_insight_search_returns_stored_revision(
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
