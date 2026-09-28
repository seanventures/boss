"""A transport key cannot claim another project's or agent's secret authority."""

from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.app.routers import agent_kms as kms_routes


PROJECT = "boss-dogfood"
OWNER = "ga-kms-owner"


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def bound_kms_keys(monkeypatch, tmp_path, engine):
    low_member = f"ga_kms_low_{uuid4().hex[:8]}"
    with engine.begin() as connection:
        api_main.members.upsert_member(
            connection,
            project_id=PROJECT,
            member_id=low_member,
            display_name="Low permission KMS caller",
            member_type="human",
            role_keys=[],
            permissions=["agent_chat"],
            actor="ceo",
        )
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", "ceo-key,low-key")
    monkeypatch.setattr(
        api_main.settings,
        "api_key_principals",
        f"ceo-key:ceo,low-key:{low_member}",
    )
    monkeypatch.setenv("BOSS_KMS_ALLOW_EPHEMERAL_MASTER", "true")
    monkeypatch.setenv("BOSS_KMS_VAULT_DIR", str(tmp_path / "vault"))
    return low_member


def test_bound_low_permission_key_cannot_list_or_reveal_other_agents_secret(
    client, bound_kms_keys,
):
    low_member = bound_kms_keys
    secret_value = "sk-test-ga-kms-protected-value"
    stored = client.post(
        "/v1/kms/keys",
        headers=_auth("ceo-key"),
        json={
            "project_id": PROJECT,
            "name": f"ga-kms-{uuid4().hex[:8]}",
            "value": secret_value,
            "owner_agent": OWNER,
            "actor": "ceo",
            "backend": "local_vault",
        },
    )
    assert stored.status_code == 201, stored.text
    key_id = stored.json()["id"]

    metadata = {"project_id": PROJECT, "agent_id": OWNER, "actor": low_member}
    assert client.get(
        "/v1/kms/keys", headers=_auth("low-key"), params=metadata
    ).status_code == 403
    assert client.get(
        f"/v1/kms/keys/{key_id}", headers=_auth("low-key"), params=metadata
    ).status_code == 403
    assert client.get(
        f"/v1/kms/keys/{key_id}/grants",
        headers=_auth("low-key"), params=metadata,
    ).status_code == 403

    lease_body = {
        "project_id": PROJECT,
        "agent_id": OWNER,
        "actor": low_member,
        "reveal": True,
    }
    assert client.post(
        f"/v1/kms/keys/{key_id}/lease",
        headers=_auth("low-key"), json=lease_body,
    ).status_code == 403
    assert client.post(
        f"/v1/kms/keys/{key_id}/lease",
        headers=_auth("low-key"), json={**lease_body, "actor": "ceo"},
    ).status_code == 403

    # Project authorization is followed by the existing agent grant check.
    ungranted = client.post(
        f"/v1/kms/keys/{key_id}/lease",
        headers=_auth("ceo-key"),
        json={"project_id": PROJECT, "agent_id": "ungranted", "actor": "ceo", "reveal": True},
    )
    assert ungranted.status_code == 403, ungranted.text

    lease = client.post(
        f"/v1/kms/keys/{key_id}/lease",
        headers=_auth("ceo-key"),
        json={"project_id": PROJECT, "agent_id": OWNER, "actor": "ceo"},
    )
    assert lease.status_code == 201, lease.text
    lease_id = lease.json()["lease_id"]
    assert lease.json()["value"] is None
    resolve = {"project_id": PROJECT, "agent_id": OWNER, "actor": low_member}
    assert client.post(
        f"/v1/kms/leases/{lease_id}/resolve",
        headers=_auth("low-key"), json=resolve,
    ).status_code == 403
    assert client.post(
        f"/v1/kms/leases/{lease_id}/resolve",
        headers=_auth("low-key"), json={**resolve, "actor": "ceo"},
    ).status_code == 403
    revealed = client.post(
        f"/v1/kms/leases/{lease_id}/resolve",
        headers=_auth("ceo-key"), json={**resolve, "actor": "ceo"},
    )
    assert revealed.status_code == 200, revealed.text
    assert revealed.json()["value"] == secret_value


def test_kms_rejects_low_permission_writes_before_service_call(
    client, bound_kms_keys, monkeypatch,
):
    low_member = bound_kms_keys
    called = []

    def unexpected(*_args, **_kwargs):
        called.append(True)
        raise AssertionError("KMS service must not run before project authorization")

    for name in ("store_key", "grant_key", "revoke_grant", "revoke_lease"):
        monkeypatch.setattr(kms_routes.kms, name, unexpected)

    headers = _auth("low-key")
    base = {"project_id": PROJECT, "actor": low_member}
    assert client.post(
        "/v1/kms/keys", headers=headers,
        json={**base, "name": "blocked", "value": "test-secret", "owner_agent": OWNER},
    ).status_code == 403
    assert client.post(
        "/v1/kms/keys/key_demo/grant", headers=headers,
        json={**base, "granter_agent": OWNER, "grantee_agent": "peer"},
    ).status_code == 403
    assert client.request(
        "DELETE", "/v1/kms/keys/key_demo/grants/peer", headers=headers,
        json={**base, "actor_agent": OWNER},
    ).status_code == 403
    assert client.post(
        "/v1/kms/leases/lease_demo/revoke", headers=headers, params=base,
    ).status_code == 403
    assert not called


def test_kms_rejects_valid_but_unbound_transport_key(
    client, bound_kms_keys, monkeypatch,
):
    monkeypatch.setattr(api_main.settings, "api_key_principals", "")
    monkeypatch.setattr(
        kms_routes.kms,
        "lease_key",
        lambda *_args, **_kwargs: pytest.fail("unbound key must not reach KMS service"),
    )
    response = client.post(
        "/v1/kms/keys/any/lease",
        headers=_auth("low-key"),
        json={"project_id": PROJECT, "agent_id": OWNER, "actor": "ceo", "reveal": True},
    )
    assert response.status_code == 403, response.text
    assert "bound" in response.json()["detail"]


def test_kms_project_grant_does_not_authorize_a_different_project(
    client, engine, bound_kms_keys, monkeypatch,
):
    low_member = bound_kms_keys
    other_project = f"ga-kms-other-{uuid4().hex[:8]}"
    with engine.begin() as connection:
        workspace_id = connection.execute(
            text("SELECT workspace_id FROM projects WHERE id = :id"), {"id": PROJECT}
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO projects (id, workspace_id, name, slug) "
                "VALUES (:id, :workspace_id, 'Other KMS project', :id)"
            ),
            {"id": other_project, "workspace_id": workspace_id},
        )
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id=low_member,
            display_name="KMS source project operator", member_type="human",
            role_keys=[], permissions=["integration_manage"], actor="ceo",
        )
    monkeypatch.setattr(
        kms_routes.kms, "lease_key",
        lambda *_args, **_kwargs: pytest.fail("foreign project KMS service must not run"),
    )
    try:
        response = client.post(
            "/v1/kms/keys/key_in_other_project/lease",
            headers=_auth("low-key"),
            json={
                "project_id": other_project,
                "agent_id": OWNER,
                "actor": low_member,
                "reveal": True,
            },
        )
        assert response.status_code == 403, response.text
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM events WHERE project_id = :id"), {"id": other_project}
            )
            connection.execute(
                text("DELETE FROM projects WHERE id = :id"), {"id": other_project}
            )
