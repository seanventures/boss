from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.services.resource_plane import ProviderCredentialError


PROJECT_ID = "boss-dogfood"


def _create_provider(client, **overrides):
    payload = {
        "project_id": PROJECT_ID,
        "provider": "digitalocean",
        "account_label": "credential-lifecycle",
        "credential_refs": {"token": "env:BOSS_TEST_PROVIDER_TOKEN_V1"},
        "read_only": True,
        "actor": "ceo",
    }
    payload.update(overrides)
    return client.post("/v1/providers", json=payload)


def test_initial_infrastructure_provider_definitions_are_truthful(client):
    response = client.get("/v1/provider-definitions")
    assert response.status_code == 200
    definitions = {item["provider"]: item for item in response.json()["items"]}

    assert set(definitions) == {"aws", "azure", "digitalocean", "gcp", "ovhcloud"}
    assert definitions["aws"]["adapter_status"] == "available"
    assert definitions["digitalocean"]["adapter_status"] == "available"
    assert definitions["azure"]["adapter_status"] == "available"
    assert definitions["ovhcloud"]["adapter_status"] == "available"
    assert definitions["gcp"]["adapter_status"] == "planned"
    ovh_slots = {
        slot["key"]
        for method in definitions["ovhcloud"]["auth_methods"]
        for slot in method["credential_slots"]
    }
    # application_key, application_secret, consumer_key (+ optional endpoint/project slots)
    assert {
        "application_key",
        "application_secret",
        "consumer_key",
    }.issubset(ovh_slots)
    assert "value" not in json.dumps(response.json()).lower()


def test_planned_provider_records_secret_refs_without_claiming_live_adapter(
    client, engine, monkeypatch
):
    def unexpected_provider_call(*_args, **_kwargs):
        raise AssertionError("planned provider must not make an outbound call")

    monkeypatch.setattr(api_main.resource_plane, "_test_connection", unexpected_provider_call)
    # GCP remains planned — use it to assert secret refs without live adapter claims.
    created = _create_provider(
        client,
        provider="gcp",
        account_label="gcp-planned",
        credential_refs={
            "service_account": "env:BOSS_TEST_GCP_SA",
        },
        connection_metadata={"project_id": "gcp-metadata-only"},
    )
    assert created.status_code == 201
    account = created.json()
    assert account["status"] == "pending_adapter"
    assert account["auth_method"] == "service_account"
    assert account["credential_ref"] == "env:BOSS_TEST_GCP_SA"
    assert set(account["credential_refs"]) == {"service_account"}

    tested = client.post(
        f"/v1/providers/{account['id']}/test",
        params={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    assert tested.status_code == 200
    assert tested.json()["status"] == "unsupported"
    assert tested.json()["provider_account"]["status"] == "pending_adapter"
    assert tested.json()["provider_health"]["status"] == "unknown"

    with engine.begin() as connection:
        refs = connection.execute(
            text(
                """
                SELECT scope_type, scope_id, secret_kind, status, metadata
                FROM secret_refs
                WHERE project_id = :project_id AND provider = 'gcp'
                ORDER BY ref_key
                """
            ),
            {"project_id": PROJECT_ID},
        ).mappings().all()
    assert len(refs) == 1
    assert {row["scope_type"] for row in refs} == {"project"}
    assert {row["scope_id"] for row in refs} == {PROJECT_ID}
    assert {row["status"] for row in refs} == {"active"}
    assert all(row["metadata"] == {"raw_secret_stored": False} for row in refs)


def test_secret_shaped_metadata_and_raw_credential_values_are_rejected(client):
    raw_metadata = client.post(
        "/v1/secret-refs",
        json={
            "project_id": PROJECT_ID,
            "ref_kind": "env",
            "ref_key": "BOSS_TEST_REJECT_METADATA",
            "provider": "digitalocean",
            "metadata": {"api_token": "do-not-store-me"},
            "actor": "ceo",
        },
    )
    assert raw_metadata.status_code == 422
    assert "SecretRef" in raw_metadata.json()["detail"]

    raw_provider_metadata = _create_provider(
        client,
        account_label="reject-raw-provider-metadata",
        connection_metadata={"client_secret": "do-not-store-me"},
    )
    assert raw_provider_metadata.status_code == 422
    assert "SecretRef" in raw_provider_metadata.json()["detail"]

    malformed_ref = _create_provider(
        client,
        account_label="reject-raw-credential",
        credential_refs={"token": "literal-provider-token"},
    )
    assert malformed_ref.status_code == 422
    assert "<kind>:<key>" in malformed_ref.json()["detail"]

    raw_env_value = _create_provider(
        client,
        account_label="reject-env-raw-value",
        credential_refs={"token": "env:sk-this-is-not-an-env-name"},
    )
    assert raw_env_value.status_code == 422
    assert "environment variable names" in raw_env_value.json()["detail"]


def test_revoked_secret_ref_cannot_be_silently_reactivated(client, engine):
    created = client.post(
        "/v1/secret-refs",
        json={
            "project_id": PROJECT_ID,
            "ref_kind": "env",
            "ref_key": "BOSS_TEST_REVOKED_PROVIDER_TOKEN",
            "provider": "digitalocean",
            "actor": "ceo",
        },
    )
    assert created.status_code == 201
    secret_ref_id = created.json()["id"]
    revoked = client.delete(
        f"/v1/secret-refs/{secret_ref_id}",
        params={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    assert revoked.status_code == 204

    provider = _create_provider(
        client,
        account_label="must-not-reactivate",
        credential_refs={"token": "env:BOSS_TEST_REVOKED_PROVIDER_TOKEN"},
    )
    assert provider.status_code == 422
    assert "cannot be silently reactivated" in provider.json()["detail"]

    cannot_patch_active = client.patch(
        f"/v1/secret-refs/{secret_ref_id}",
        json={
            "project_id": PROJECT_ID,
            "status": "active",
            "actor": "ceo",
        },
    )
    assert cannot_patch_active.status_code == 422
    assert "cannot be reactivated" in cannot_patch_active.json()["detail"]

    with engine.begin() as connection:
        status = connection.execute(
            text("SELECT status FROM secret_refs WHERE id = :id"),
            {"id": secret_ref_id},
        ).scalar_one()
    assert status == "revoked"


def test_multi_slot_rotation_records_overlap_and_lineage(client, engine):
    created = _create_provider(
        client,
        provider="aws",
        account_label="aws-rotate",
        credential_refs={
            "access_key_id": "env:BOSS_TEST_AWS_ACCESS_V1",
            "secret_access_key": "env:BOSS_TEST_AWS_SECRET_V1",
        },
    )
    assert created.status_code == 201
    account = created.json()
    assert account["credential_version"] == 1

    bypass_rotation = _create_provider(
        client,
        provider="aws",
        account_label="aws-rotate",
        credential_refs={
            "access_key_id": "env:BOSS_TEST_AWS_ACCESS_V2",
            "secret_access_key": "env:BOSS_TEST_AWS_SECRET_V2",
        },
    )
    assert bypass_rotation.status_code == 422
    assert "rotation endpoint" in bypass_rotation.json()["detail"]

    rotated = client.post(
        f"/v1/providers/{account['id']}/rotate",
        json={
            "project_id": PROJECT_ID,
            "credential_refs": {
                "access_key_id": "env:BOSS_TEST_AWS_ACCESS_V2",
                "secret_access_key": "env:BOSS_TEST_AWS_SECRET_V2",
            },
            "overlap_minutes": 30,
            "actor": "ceo",
        },
    )
    assert rotated.status_code == 200
    payload = rotated.json()
    assert payload["provider_account"]["credential_version"] == 2
    assert payload["provider_account"]["credential_ref"] == "env:BOSS_TEST_AWS_SECRET_V2"
    assert {item["status"] for item in payload["prior_secret_refs"]} == {"rotating"}
    assert {item["status"] for item in payload["active_secret_refs"]} == {"active"}
    assert all(
        item["overlap_expires_at"] == payload["overlap_expires_at"]
        for item in payload["prior_secret_refs"]
    )
    assert all(
        item["supersedes_secret_ref_id"]
        for item in payload["active_secret_refs"]
    )

    with engine.begin() as connection:
        event = connection.execute(
            text(
                """
                SELECT data
                FROM events
                WHERE project_id = :project_id
                  AND event_type = 'provider.credentials_rotated'
                  AND entity_id = :account_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"project_id": PROJECT_ID, "account_id": account["id"]},
        ).scalar_one()
    assert event["credential_version"] == 2
    assert event["rotated_slots"] == ["access_key_id", "secret_access_key"]
    assert "BOSS_TEST_AWS_SECRET_V2" not in json.dumps(event)


def test_expired_rotation_overlap_secret_ref_is_not_usable(client, engine):
    created = _create_provider(
        client,
        account_label="expired-overlap",
        credential_refs={"token": "env:BOSS_TEST_EXPIRED_TOKEN_V1"},
    ).json()
    rotated = client.post(
        f"/v1/providers/{created['id']}/rotate",
        json={
            "project_id": PROJECT_ID,
            "credential_refs": {"token": "env:BOSS_TEST_EXPIRED_TOKEN_V2"},
            "overlap_minutes": 0,
            "actor": "ceo",
        },
    )
    assert rotated.status_code == 200
    with engine.begin() as connection:
        with pytest.raises(ValueError, match="overlap has expired"):
            api_main.secret_refs.require_usable(
                connection,
                project_id=PROJECT_ID,
                credential_ref="env:BOSS_TEST_EXPIRED_TOKEN_V1",
            )


def test_disable_and_revoke_block_provider_use_before_adapter_call(
    client, engine, monkeypatch
):
    provider = _create_provider(client, account_label="lifecycle-blocks").json()

    disabled = client.post(
        f"/v1/providers/{provider['id']}/disable",
        json={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"

    def unexpected_provider_call(*_args, **_kwargs):
        raise AssertionError("disabled or revoked account reached provider adapter")

    monkeypatch.setattr(api_main.resource_plane, "_test_connection", unexpected_provider_call)
    blocked_test = client.post(
        f"/v1/providers/{provider['id']}/test",
        params={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    assert blocked_test.status_code == 409

    blocked_sync = client.post(
        "/v1/resources/sync",
        json={
            "project_id": PROJECT_ID,
            "provider_account_id": provider["id"],
            "actor": "ceo",
        },
    )
    assert blocked_sync.status_code == 200
    assert blocked_sync.json()["runs"][0]["status"] == "failed"
    assert "disabled" in blocked_sync.json()["runs"][0]["error"]["message"]

    enabled = client.post(
        f"/v1/providers/{provider['id']}/enable",
        json={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    assert enabled.status_code == 200
    assert enabled.json()["status"] == "active"

    revoked = client.post(
        f"/v1/providers/{provider['id']}/revoke",
        json={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    assert revoked.json()["read_only"] is True
    assert revoked.json()["deploy_capable"] is False
    assert revoked.json()["destructive_actions_disabled"] is True

    with engine.begin() as connection:
        account = api_main.resource_plane.get_provider_account(
            connection, PROJECT_ID, provider["id"]
        )
        with pytest.raises(ProviderCredentialError, match="revoked"):
            api_main.resource_plane._execute_provider_operation(
                connection,
                account,
                {
                    "skill_id": "digitalocean.droplet.create",
                    "action": "create",
                    "destructive": False,
                },
            )

    cannot_enable = client.post(
        f"/v1/providers/{provider['id']}/enable",
        json={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    assert cannot_enable.status_code == 409
    assert "cannot be re-enabled" in cannot_enable.json()["detail"]


def test_provider_update_is_audited_and_never_accepts_secret_material(client, engine):
    provider = _create_provider(client, account_label="account-update").json()
    updated = client.patch(
        f"/v1/providers/{provider['id']}",
        json={
            "project_id": PROJECT_ID,
            "account_label": "account-update-renamed",
            "scopes": ["read", "billing:read"],
            "billing_readable": True,
            "connection_metadata": {"region_policy": ["nyc3", "sfo3"]},
            "owner_actor": "cfo",
            "actor": "ceo",
        },
    )
    assert updated.status_code == 200
    payload = updated.json()
    assert payload["account_label"] == "account-update-renamed"
    assert payload["billing_readable"] is True
    assert payload["owner_actor"] == "cfo"

    with engine.begin() as connection:
        event = connection.execute(
            text(
                """
                SELECT data
                FROM events
                WHERE project_id = :project_id
                  AND event_type = 'provider.account_updated'
                  AND entity_id = :account_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"project_id": PROJECT_ID, "account_id": provider["id"]},
        ).scalar_one()
    assert "billing_readable" in event["changed_fields"]
    assert "owner_actor" in event["changed_fields"]
    assert "credential_ref" not in event


def test_failed_connection_test_redacts_provider_exception_details(
    client, monkeypatch
):
    provider = _create_provider(client, account_label="redacted-error").json()
    monkeypatch.setenv("BOSS_TEST_PROVIDER_TOKEN_V1", "super-secret-provider-token")

    def fail_with_secret(_provider, credential):
        raise RuntimeError(f"upstream echoed {credential}")

    monkeypatch.setattr(api_main.resource_plane, "_test_connection", fail_with_secret)
    tested = client.post(
        f"/v1/providers/{provider['id']}/test",
        params={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    assert tested.status_code == 200
    body = json.dumps(tested.json())
    assert tested.json()["status"] == "failed"
    assert "super-secret-provider-token" not in body
    assert "upstream echoed" not in body


def test_sync_failure_redacts_provider_credential_from_evidence(
    client, engine, monkeypatch
):
    provider = _create_provider(client, account_label="redacted-sync-error").json()
    credential = "super-secret-sync-provider-token"
    monkeypatch.setenv("BOSS_TEST_PROVIDER_TOKEN_V1", credential)

    def fail_with_secret(_provider, resolved_credential):
        raise RuntimeError(f"inventory failure echoed {resolved_credential}")

    monkeypatch.setattr(api_main.resource_plane, "_sync_inventory", fail_with_secret)
    synced = client.post(
        "/v1/resources/sync",
        json={
            "project_id": PROJECT_ID,
            "provider_account_id": provider["id"],
            "actor": "ceo",
        },
    )
    assert synced.status_code == 200
    body = json.dumps(synced.json())
    assert credential not in body
    assert "[REDACTED]" in body

    with engine.begin() as connection:
        evidence = connection.execute(
            text(
                """
                SELECT reason, payload
                FROM deadletters
                WHERE project_id = :project_id
                  AND source_kind = 'provider_sync'
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"project_id": PROJECT_ID},
        ).mappings().one()
        event_data = connection.execute(
            text(
                """
                SELECT data
                FROM events
                WHERE project_id = :project_id
                  AND event_type = 'provider.sync_failed'
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"project_id": PROJECT_ID},
        ).scalar_one()
    assert credential not in json.dumps(dict(evidence))
    assert credential not in json.dumps(event_data)
    assert "[REDACTED]" in evidence["reason"]
