from __future__ import annotations

import hashlib

from sqlalchemy import text
from boss_engine.services import model_gateway as model_gateway_module


PROJECT_ID = "boss-dogfood"


def _lease_job(client, suffix: str) -> dict:
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": PROJECT_ID,
            "kind": f"model.gateway.test.{suffix}",
            "payload": {"purpose": "model gateway acceptance test"},
            "workload_class": "owner_sandbox",
            "data_boundary": "internal",
        },
    )
    assert created.status_code == 201, created.text
    leased = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": f"worker-{suffix}",
            "capabilities": [f"model.gateway.test.{suffix}"],
            "lease_ttl_seconds": 600,
        },
    )
    assert leased.status_code == 200, leased.text
    return leased.json()["job"]


def _bind_openai_api_secret(client, suffix: str = "e2e") -> str:
    created = client.post(
        "/v1/secret-refs",
        json={
            "project_id": PROJECT_ID,
            "ref_kind": "env",
            "ref_key": f"BOSS_TEST_OPENAI_{suffix.upper()}",
            "provider": "openai",
            "secret_kind": "api_token",
            "status": "active",
            "actor": "ceo",
        },
    )
    assert created.status_code == 201, created.text
    secret_ref_id = created.json()["id"]
    bound = client.post(
        "/v1/entitlements/sources/openai_api/credential-ref",
        json={
            "project_id": PROJECT_ID,
            "secret_ref_id": secret_ref_id,
            "actor": "ceo",
        },
    )
    assert bound.status_code == 200, bound.text
    assert bound.json()["credential_secret_ref_id"] == secret_ref_id
    assert bound.json()["effective_status"] == "active"
    return secret_ref_id


def test_paid_model_gateway_lease_invocation_and_settlement_are_auditable(
    client, engine
):
    secret_ref_id = _bind_openai_api_secret(client)
    job = _lease_job(client, "paid-e2e")

    acquired = client.post(
        "/v1/model-gateway/leases",
        json={
            "project_id": PROJECT_ID,
            "job_id": job["id"],
            "worker_id": "worker-paid-e2e",
            "job_lease_token": job["lease_token"],
            "agent_id": "backend_engineer",
            "skill_id": "model.generate_text",
            "preferred_capabilities": ["coding"],
            "risk_tier": 1,
            "consumer_owner_id": PROJECT_ID,
            "estimated_cost_usd_micros": 2_000,
            "max_usage": {"total_tokens": 10_000},
            "dedupe_key": "paid-e2e-capacity",
            "ttl_seconds": 300,
        },
    )
    assert acquired.status_code == 201, acquired.text
    capacity = acquired.json()
    assert capacity["duplicate"] is False
    assert capacity["lease_token"]
    assert capacity["lease"]["credential_secret_ref_id"] == secret_ref_id
    assert capacity["lease"]["budget_reservation_id"]
    assert "lease_token_hash" not in capacity["lease"]

    heartbeat = client.post(
        f"/v1/model-gateway/leases/{capacity['lease']['id']}/heartbeat",
        json={
            "project_id": PROJECT_ID,
            "worker_id": "worker-paid-e2e",
            "lease_token": capacity["lease_token"],
            "job_lease_token": job["lease_token"],
            "ttl_seconds": 300,
        },
    )
    assert heartbeat.status_code == 200, heartbeat.text
    assert heartbeat.json()["status"] == "active"

    request_hash = hashlib.sha256(b"prompt remains outside the ledger").hexdigest()
    started = client.post(
        f"/v1/model-gateway/leases/{capacity['lease']['id']}/invocations",
        json={
            "project_id": PROJECT_ID,
            "worker_id": "worker-paid-e2e",
            "lease_token": capacity["lease_token"],
            "job_lease_token": job["lease_token"],
            "request_hash": request_hash,
            "dedupe_key": "paid-e2e-invocation",
            "metadata": {"stage": "implementation", "trace_id": "trace-paid-e2e"},
        },
    )
    assert started.status_code == 201, started.text
    invocation = started.json()["invocation"]
    assert invocation["status"] == "in_progress"

    settled = client.post(
        f"/v1/model-gateway/leases/{capacity['lease']['id']}/invocations/"
        f"{invocation['id']}/settle",
        json={
            "project_id": PROJECT_ID,
            "worker_id": "worker-paid-e2e",
            "lease_token": capacity["lease_token"],
            "job_lease_token": job["lease_token"],
            "succeeded": True,
            "provider_ref": "openai-request-123",
            "usage_units": {
                "prompt_tokens": 100,
                "completion_tokens": 40,
                "total_tokens": 140,
            },
            "actual_cost_usd_micros": 1_500,
            "observed_capacity": {
                "capacity_state": "available",
                "remaining_requests": 99,
            },
        },
    )
    assert settled.status_code == 200, settled.text
    assert settled.json()["status"] == "succeeded"

    with engine.begin() as connection:
        evidence = connection.execute(
            text(
                """
                SELECT mgl.status AS lease_status, br.status AS reservation_status,
                       r.status AS receipt_status, count(cr.id) AS cost_receipts,
                       count(llm.id) AS llm_calls, count(cap.id) AS capacity_receipts
                FROM model_gateway_leases mgl
                JOIN budget_reservations br ON br.id=mgl.budget_reservation_id
                JOIN model_gateway_invocations mgi ON mgi.model_gateway_lease_id=mgl.id
                JOIN receipts r ON r.id=mgi.receipt_id
                LEFT JOIN cost_receipts cr ON cr.receipt_id=r.id
                LEFT JOIN llm_calls llm ON llm.project_id=mgl.project_id
                  AND llm.engine=mgl.source_id
                LEFT JOIN capacity_receipts cap ON cap.model_route_decision_id=mgl.route_decision_id
                WHERE mgl.id=:lease_id
                GROUP BY mgl.status, br.status, r.status
                """
            ),
            {"lease_id": capacity["lease"]["id"]},
        ).mappings().one()
    assert evidence["lease_status"] == "completed"
    assert evidence["reservation_status"] == "consumed"
    assert evidence["receipt_status"] == "succeeded"
    assert int(evidence["cost_receipts"]) >= 1
    assert int(evidence["llm_calls"]) >= 1
    assert int(evidence["capacity_receipts"]) >= 1

    leases = client.get(
        "/v1/model-gateway/leases",
        params={"project_id": PROJECT_ID, "job_id": job["id"]},
    )
    assert leases.status_code == 200
    assert leases.json()["items"][0]["status"] == "completed"
    assert "lease_token" not in leases.text
    invocations = client.get(
        "/v1/model-gateway/invocations",
        params={"project_id": PROJECT_ID, "lease_id": capacity["lease"]["id"]},
    )
    assert invocations.status_code == 200
    assert invocations.json()["items"][0]["receipt_id"] == invocation["receipt_id"]
    assert "prompt remains outside" not in invocations.text


def test_subscription_tokens_cannot_be_uploaded_or_pooled(client):
    rejected_upload = client.post(
        "/v1/entitlements/sources/openai_codex_chatgpt/credential",
        json={
            "project_id": PROJECT_ID,
            "value": "must-never-be-stored",
            "backend": "platform_secret_store",
            "actor": "ceo",
        },
    )
    assert rejected_upload.status_code == 422
    assert "official connection session" in rejected_upload.json()["detail"]
    assert "must-never-be-stored" not in rejected_upload.text

    connected = client.post(
        "/v1/entitlements/sources/openai_codex_chatgpt/connect",
        json={
            "project_id": PROJECT_ID,
            "connection_ref": "local:codex-login",
            "auth_method": "official_cli_oauth",
            "actor": "ceo",
        },
    )
    assert connected.status_code == 200, connected.text
    job = _lease_job(client, "owner-scope")
    denied = client.post(
        "/v1/model-gateway/leases",
        json={
            "project_id": PROJECT_ID,
            "job_id": job["id"],
            "worker_id": "worker-owner-scope",
            "job_lease_token": job["lease_token"],
            "agent_id": "backend_engineer",
            "skill_id": "model.generate_text",
            "preferred_capabilities": ["coding"],
            "risk_tier": 1,
            "consumer_owner_id": "another-customer",
            "estimated_cost_usd_micros": 0,
            "dedupe_key": "owner-scope-denial",
        },
    )
    assert denied.status_code == 409
    assert "owner scoped" in denied.json()["detail"]


def test_manual_api_credential_is_write_through_and_returns_metadata_only(
    client, engine, monkeypatch
):
    raw_value = "sk-test-write-through-only"

    def fake_put_secret(connection, _settings, **kwargs):
        assert kwargs["value"] == raw_value
        secret_ref_id = "sref_model_upload_test"
        connection.execute(
            text(
                """
                INSERT INTO secret_refs (
                  id,project_id,scope_type,scope_id,ref_kind,ref_key,provider,
                  secret_kind,storage_backend,status,metadata,created_by
                ) VALUES (
                  :id,:p,'project',:p,'platform_secret_store',
                  'BOSS_MODEL_OPENAI_API_CREDENTIAL','api_token','api_token',
                  'platform_secret_store','active',
                  '{"raw_secret_stored": false, "last4": "only"}'::jsonb,:actor
                )
                """
            ),
            {"id": secret_ref_id, "p": kwargs["project_id"], "actor": kwargs["actor"]},
        )
        return {
            "secret_ref_id": secret_ref_id,
            "backend": "platform_secret_store",
            "fingerprint": None,
            "last4": "only",
            "raw_secret_stored": False,
        }

    monkeypatch.setattr(model_gateway_module, "put_secret", fake_put_secret)
    response = client.post(
        "/v1/entitlements/sources/openai_api/credential",
        json={
            "project_id": PROJECT_ID,
            "value": raw_value,
            "backend": "platform_secret_store",
            "actor": "ceo",
        },
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["secret_ref_id"] == "sref_model_upload_test"
    assert payload["raw_secret_stored"] is False
    assert raw_value not in response.text
    with engine.begin() as connection:
        provider, raw_persisted = connection.execute(
            text(
                """
                SELECT provider,
                       position(:raw_value in coalesce(metadata::text, '')) > 0
                FROM secret_refs WHERE id='sref_model_upload_test'
                """
            ),
            {"raw_value": raw_value},
        ).one()
    assert provider == "openai"
    assert raw_persisted is False


def test_model_gateway_rejects_prompt_like_invocation_metadata(client):
    _bind_openai_api_secret(client, "metadata")
    job = _lease_job(client, "metadata")
    acquired = client.post(
        "/v1/model-gateway/leases",
        json={
            "project_id": PROJECT_ID,
            "job_id": job["id"],
            "worker_id": "worker-metadata",
            "job_lease_token": job["lease_token"],
            "agent_id": "qa_engineer",
            "skill_id": "model.generate_text",
            "preferred_capabilities": ["coding"],
            "estimated_cost_usd_micros": 1_000,
            "dedupe_key": "metadata-capacity",
        },
    ).json()
    rejected = client.post(
        f"/v1/model-gateway/leases/{acquired['lease']['id']}/invocations",
        json={
            "project_id": PROJECT_ID,
            "worker_id": "worker-metadata",
            "lease_token": acquired["lease_token"],
            "job_lease_token": job["lease_token"],
            "request_hash": "a" * 64,
            "dedupe_key": "metadata-invocation",
            "metadata": {"prompt": "do not retain me"},
        },
    )
    assert rejected.status_code == 422
    assert "metadata.prompt is not allowed" in rejected.json()["detail"]


def test_model_gateway_reaper_releases_expired_paid_capacity(client, engine):
    _bind_openai_api_secret(client, "reaper")
    job = _lease_job(client, "reaper")
    acquired = client.post(
        "/v1/model-gateway/leases",
        json={
            "project_id": PROJECT_ID,
            "job_id": job["id"],
            "worker_id": "worker-reaper",
            "job_lease_token": job["lease_token"],
            "agent_id": "operator",
            "skill_id": "model.generate_text",
            "estimated_cost_usd_micros": 1_000,
            "dedupe_key": "reaper-capacity",
        },
    ).json()
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE model_gateway_leases "
                "SET expires_at=now() - interval '1 second' WHERE id=:id"
            ),
            {"id": acquired["lease"]["id"]},
        )

    projected = client.get(
        "/v1/model-gateway/leases",
        params={"project_id": PROJECT_ID, "job_id": job["id"]},
    )
    assert projected.json()["items"][0]["status"] == "expired"
    reaped = client.post(
        "/v1/model-gateway/leases/reap",
        json={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    assert reaped.status_code == 200, reaped.text
    assert reaped.json()["reaped"] == 1
    with engine.begin() as connection:
        lease_status, reservation_status = connection.execute(
            text(
                """
                SELECT mgl.status, br.status
                FROM model_gateway_leases mgl
                JOIN budget_reservations br ON br.id=mgl.budget_reservation_id
                WHERE mgl.id=:id
                """
            ),
            {"id": acquired["lease"]["id"]},
        ).one()
    assert lease_status == "expired"
    assert reservation_status == "released"


def test_failed_unbilled_invocation_is_visible_and_releases_budget(client, engine):
    _bind_openai_api_secret(client, "failure")
    job = _lease_job(client, "failure")
    acquired = client.post(
        "/v1/model-gateway/leases",
        json={
            "project_id": PROJECT_ID,
            "job_id": job["id"],
            "worker_id": "worker-failure",
            "job_lease_token": job["lease_token"],
            "agent_id": "qa_engineer",
            "skill_id": "model.generate_text",
            "estimated_cost_usd_micros": 1_000,
            "dedupe_key": "failure-capacity",
        },
    ).json()
    started = client.post(
        f"/v1/model-gateway/leases/{acquired['lease']['id']}/invocations",
        json={
            "project_id": PROJECT_ID,
            "worker_id": "worker-failure",
            "lease_token": acquired["lease_token"],
            "job_lease_token": job["lease_token"],
            "request_hash": "b" * 64,
            "dedupe_key": "failure-invocation",
            "metadata": {"stage": "qa"},
        },
    ).json()["invocation"]
    failed = client.post(
        f"/v1/model-gateway/leases/{acquired['lease']['id']}/invocations/"
        f"{started['id']}/settle",
        json={
            "project_id": PROJECT_ID,
            "worker_id": "worker-failure",
            "lease_token": acquired["lease_token"],
            "job_lease_token": job["lease_token"],
            "succeeded": False,
            "usage_units": {},
            "actual_cost_usd_micros": 0,
            "error": {"kind": "provider_unavailable", "retryable": True},
            "observed_capacity": {"capacity_state": "unavailable"},
        },
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["status"] == "failed"
    with engine.begin() as connection:
        statuses = connection.execute(
            text(
                """
                SELECT mgl.status AS lease_status, br.status AS reservation_status,
                       r.status AS receipt_status, mgi.error->>'kind' AS error_kind
                FROM model_gateway_leases mgl
                JOIN budget_reservations br ON br.id=mgl.budget_reservation_id
                JOIN model_gateway_invocations mgi ON mgi.model_gateway_lease_id=mgl.id
                JOIN receipts r ON r.id=mgi.receipt_id
                WHERE mgl.id=:id
                """
            ),
            {"id": acquired["lease"]["id"]},
        ).mappings().one()
    assert dict(statuses) == {
        "lease_status": "failed",
        "reservation_status": "released",
        "receipt_status": "failed",
        "error_kind": "provider_unavailable",
    }
