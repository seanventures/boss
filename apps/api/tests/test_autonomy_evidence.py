def _record(client, evidence_type: str, evidence_ref: str):
    return client.post(
        "/v1/roles/metatron/trust-evidence",
        json={
            "project_id": "boss-dogfood",
            "actor": "vp_agents",
            "evidence_type": evidence_type,
            "evidence_ref": evidence_ref,
            "detail": {"source": "independent_qa", "measurement": "fixture"},
        },
    )


def test_trust_evidence_is_idempotent_and_evaluation_counts_all_evidence(client):
    first = _record(client, "duplicate_effect_attempt", "receipt:duplicate-001")
    assert first.status_code == 201
    assert first.json()["recorded"] is True
    assert first.json()["event_type"] == "role.duplicate_effect_attempt"

    repeated = _record(client, "duplicate_effect_attempt", "receipt:duplicate-001")
    assert repeated.status_code == 201
    assert repeated.json() == {**first.json(), "recorded": False}

    assert _record(client, "hidden_failure", "job:hidden-failure-001").status_code == 201
    assert _record(client, "low_quality_output", "eval:quality-001").status_code == 201

    evaluation = client.post(
        "/v1/roles/metatron/autonomy/evaluate",
        json={"project_id": "boss-dogfood", "actor": "vp_agents"},
    )
    assert evaluation.status_code == 201
    payload = evaluation.json()
    assert payload["evidence"]["duplicate_effect_attempts"] == 1
    assert payload["evidence"]["hidden_failures"] == 1
    assert payload["evidence"]["low_quality_outputs"] == 1
    assert payload["role"]["duplicate_effect_attempt_count"] == 1
    assert payload["role"]["hidden_failure_count"] == 1
    assert payload["role"]["low_quality_output_count"] == 1
    assert payload["recent_reviews"][0]["governance_outcome"] == "auto_downgraded"


def test_successful_run_evidence_requires_unique_proof_and_only_proposes_upgrade(client):
    for number in range(30):
        response = _record(client, "successful_run", f"job:verified-{number:02d}")
        assert response.status_code == 201
        assert response.json()["recorded"] is True

    evaluation = client.post(
        "/v1/roles/metatron/autonomy/evaluate",
        json={"project_id": "boss-dogfood", "actor": "vp_agents"},
    )
    assert evaluation.status_code == 201
    payload = evaluation.json()
    assert payload["evidence"]["successful_runs"] == 30
    assert payload["recent_reviews"][0]["governance_outcome"] == "upgrade_recommended"
    assert payload["pending_autonomy_change"]["status"] == "pending_decision"
    assert payload["role"]["autonomy_level"] == 1
