"""E2E: agent KMS — store, grant, lease, resolve (local_vault backend)."""

from __future__ import annotations


import pytest

PROJECT = "boss-dogfood"
OWNER = "agent-docs"
PEER = "agent-deploy"
ACTOR = "ceo"


@pytest.fixture(autouse=True)
def _ephemeral_kms_master(monkeypatch, tmp_path):
    monkeypatch.setenv("BOSS_KMS_ALLOW_EPHEMERAL_MASTER", "true")
    monkeypatch.setenv("BOSS_KMS_VAULT_DIR", str(tmp_path / "vault"))
    # Prefer local_vault in tests
    yield


def test_backends_catalog(client):
    r = client.get("/v1/kms/backends")
    assert r.status_code == 200, r.text
    ids = {i["id"] for i in r.json()["items"]}
    assert {"local_vault", "vault", "openbao", "file", "environment"} <= ids


def test_health(client):
    r = client.get("/v1/kms/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["product"] == "boss_agent_kms"
    local = next(b for b in body["backends"] if b["id"] == "local_vault")
    assert local.get("ok") is True


def test_store_list_grant_lease_resolve(client):
    secret_value = "sk-test-super-secret-value-xyz"

    store = client.post(
        "/v1/kms/keys",
        json={
            "project_id": PROJECT,
            "name": "openai-dogfood",
            "value": secret_value,
            "owner_agent": OWNER,
            "actor": ACTOR,
            "purpose": "model",
            "backend": "local_vault",
            "secret_kind": "api_token",
        },
    )
    assert store.status_code == 201, store.text
    body = store.json()
    assert body["raw_secret_stored"] is False
    assert body["last4"] == secret_value[-4:]
    assert "value" not in body
    key_id = body["id"]

    # Owner can list (metadata only)
    listed = client.get(
        "/v1/kms/keys", params={"project_id": PROJECT, "agent_id": OWNER}
    )
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert any(i["id"] == key_id for i in items)
    assert all("value" not in i for i in items)

    # Peer cannot use yet
    denied = client.post(
        f"/v1/kms/keys/{key_id}/lease",
        json={
            "project_id": PROJECT,
            "agent_id": PEER,
            "actor": ACTOR,
            "reveal": True,
        },
    )
    assert denied.status_code == 403, denied.text

    # Owner grants use to peer
    grant = client.post(
        f"/v1/kms/keys/{key_id}/grant",
        json={
            "project_id": PROJECT,
            "granter_agent": OWNER,
            "grantee_agent": PEER,
            "permissions": ["use", "read_meta"],
            "actor": ACTOR,
        },
    )
    assert grant.status_code == 201, grant.text
    assert "use" in grant.json()["permissions"]

    # Peer lease without reveal
    lease = client.post(
        f"/v1/kms/keys/{key_id}/lease",
        json={
            "project_id": PROJECT,
            "agent_id": PEER,
            "actor": ACTOR,
            "purpose": "code-task-model",
            "ttl_seconds": 60,
            "reveal": False,
            "max_reveals": 2,
        },
    )
    assert lease.status_code == 201, lease.text
    lease_body = lease.json()
    assert lease_body["value"] is None
    lease_id = lease_body["lease_id"]

    # Resolve reveals material
    resolved = client.post(
        f"/v1/kms/leases/{lease_id}/resolve",
        json={"project_id": PROJECT, "agent_id": PEER, "actor": ACTOR},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["value"] == secret_value

    # Second resolve allowed (max_reveals=2)
    resolved2 = client.post(
        f"/v1/kms/leases/{lease_id}/resolve",
        json={"project_id": PROJECT, "agent_id": PEER, "actor": ACTOR},
    )
    assert resolved2.status_code == 200, resolved2.text

    # Third fails
    resolved3 = client.post(
        f"/v1/kms/leases/{lease_id}/resolve",
        json={"project_id": PROJECT, "agent_id": PEER, "actor": ACTOR},
    )
    assert resolved3.status_code == 403, resolved3.text

    # Owner can lease with reveal inline
    inline = client.post(
        f"/v1/kms/keys/{key_id}/lease",
        json={
            "project_id": PROJECT,
            "agent_id": OWNER,
            "actor": ACTOR,
            "reveal": True,
            "ttl_seconds": 30,
        },
    )
    assert inline.status_code == 201, inline.text
    assert inline.json()["value"] == secret_value


def test_list_does_not_include_values(client):
    client.post(
        "/v1/kms/keys",
        json={
            "project_id": PROJECT,
            "name": "meta-only-check",
            "value": "never-list-this-value-1234",
            "owner_agent": OWNER,
            "actor": ACTOR,
            "backend": "local_vault",
        },
    )
    r = client.get("/v1/kms/keys", params={"project_id": PROJECT, "agent_id": OWNER})
    assert r.status_code == 200
    blob = r.text
    assert "never-list-this-value-1234" not in blob
