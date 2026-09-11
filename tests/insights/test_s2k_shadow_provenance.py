def test_discovered_source_gets_engine_owned_provenance_without_creating_work(
    client, auth_headers
):
    candidate = client.post(
        "/insight-candidates",
        json={
            "origin": "discovered",
            "subject": "Gigabud work-profile evasion",
            "question_ids": ["android-enterprise-isolation"],
            "source_ids": [],
            "policy_revision": "shadow-v1",
        },
        headers={**auth_headers, "Idempotency-Key": "gigabud-candidate"},
    )
    assert candidate.status_code == 201

    source = client.post(
        "/insight-sources",
        json={
            "candidate_id": candidate.json()["candidate_id"],
            "origin": "discovered",
            "content_hash": "a0cfb76fec07d0f8b414f49d9afebb56003b65ef941025e9fafd6f80cde6a243",
            "acquisition_status": "ok",
            "retrieved_at": "2026-09-10T12:00:00+00:00",
            "content": "Gigabud source body.",
            "url": "HTTPS://Gigabud.Example:443/security/android-work-profile#fragment",
        },
        headers={**auth_headers, "Idempotency-Key": "gigabud-source"},
    )

    assert source.status_code == 201
    assert source.json()["origin_reference"] == (
        "shadow-source:7b2ef05c4f6adfabfafdd9fc3851824976c3536a1ed83bb487d6b72bd77f7b12"
    )
    operations = client.get("/insight-operations", headers=auth_headers)
    assert operations.json()["jobs"] == {}


def test_source_intake_rejects_browser_supplied_shadow_provenance(client, auth_headers):
    response = client.post(
        "/insight-sources",
        json={
            "candidate_id": "not-used",
            "origin": "discovered",
            "content_hash": "a" * 64,
            "acquisition_status": "fetch_failed",
            "retrieved_at": "2026-09-10T12:00:00+00:00",
            "origin_reference": "shadow-source:forged",
        },
        headers={**auth_headers, "Idempotency-Key": "forged-provenance"},
    )

    assert response.status_code == 422
