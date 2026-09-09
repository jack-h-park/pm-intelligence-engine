import hashlib


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
