def _contract():
    return {
        "required_result_fields": ["artifact_id"],
        "min_evidence_refs": 2,
        "independent_verifier_required": True,
    }


def _create_and_lease(client):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "test.completion-contract",
            "completion_contract": _contract(),
        },
    )
    assert created.status_code == 201
    leased = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "builder-worker",
            "capabilities": ["test.completion-contract"],
            "lease_ttl_seconds": 120,
        },
    )
    assert leased.status_code == 200
    return created.json(), leased.json()["job"]


def test_completion_contract_rejects_unproven_success_and_keeps_lease(client):
    created, lease = _create_and_lease(client)

    rejected = client.post(
        "/v1/worker/complete",
        json={
            "worker_id": "builder-worker",
            "job_id": lease["id"],
            "lease_token": lease["lease_token"],
            "result": {"artifact_id": "art_unverified"},
        },
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "verification_pending"
    assert rejected.json()["proof_disposition"] == "rejected"
    assert "completion_proof is required." in rejected.json()["proof_rejection_reasons"]

    jobs = client.get(
        "/v1/jobs",
        params={"project_id": "boss-dogfood", "kind": "test.completion-contract"},
    )
    assert jobs.status_code == 200
    job = next(item for item in jobs.json()["items"] if item["id"] == created["id"])
    assert job["status"] == "leased"
    assert job["last_error"]["code"] == "completion_proof_rejected"


def test_completion_contract_persists_independent_verified_proof_before_success(client):
    created, lease = _create_and_lease(client)

    rejected = client.post(
        "/v1/worker/complete",
        json={
            "worker_id": "builder-worker",
            "job_id": lease["id"],
            "lease_token": lease["lease_token"],
            "result": {
                "artifact_id": "art_candidate",
                "completion_proof": {
                    "disposition": "verified",
                    "verifier_actor": "builder-worker",
                    "evidence_refs": ["test:one", "test:two"],
                },
            },
        },
    )
    assert rejected.status_code == 200
    assert rejected.json()["proof_disposition"] == "rejected"
    assert "must differ from the completing worker" in " ".join(
        rejected.json()["proof_rejection_reasons"]
    )

    completed = client.post(
        "/v1/worker/complete",
        json={
            "worker_id": "builder-worker",
            "job_id": lease["id"],
            "lease_token": lease["lease_token"],
            "result": {
                "artifact_id": "art_candidate",
                "completion_proof": {
                    "disposition": "verified",
                    "verifier_actor": "qa-worker",
                    "evidence_refs": ["test:one", "test:two"],
                },
            },
        },
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "succeeded"
    assert completed.json()["proof_disposition"] == "accepted"
    proof_artifact_id = completed.json()["proof_artifact_id"]
    assert proof_artifact_id

    artifact = client.get(f"/v1/artifacts/{proof_artifact_id}")
    assert artifact.status_code == 200
    assert artifact.json()["artifact_type"] == "job_completion_proof"
    assert artifact.json()["content"]["proof"]["verifier_actor"] == "qa-worker"

    jobs = client.get(
        "/v1/jobs",
        params={"project_id": "boss-dogfood", "kind": "test.completion-contract"},
    )
    job = next(item for item in jobs.json()["items"] if item["id"] == created["id"])
    assert job["status"] == "succeeded"
    assert job["last_error"] == {}

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "job.completion_proof_rejected" in event_types
    assert "job.succeeded" in event_types
