"""Direct fleet writes must stop before key, SSH, or host state effects."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.services import deployment_fleet


HOME = "boss-dogfood"
WRITE_ACTIONS = ("install-key", "provision", "retire")


def _host(client, suffix: str) -> str:
    created = client.post(
        "/v1/deployment-hosts",
        json={"project_id": HOME, "name": f"gate-{suffix}",
              "address": "203.0.113.19", "actor": "ceo"},
    )
    assert created.status_code == 200, created.text
    return created.json()["id"]


@pytest.mark.parametrize("edition", ["community", "oss", "local", "unknown"])
def test_fleet_service_blocks_before_any_database_or_host_access(edition):
    settings = SimpleNamespace(boss_edition=edition, ssh_host_deploy_allowed=True)
    assert not deployment_fleet.direct_host_writes_allowed(settings)
    for service in (
        deployment_fleet.install_generated_key,
        deployment_fleet.provision_host,
        deployment_fleet.retire_host,
    ):
        with pytest.raises(PermissionError, match="Direct deployment-host changes"):
            service(object(), settings, host_id="dh_nonexistent")


def test_fleet_service_requires_explicit_outward_ssh_switch():
    settings = SimpleNamespace(
        boss_edition="enterprise_self_hosted", ssh_host_deploy_allowed=False,
    )
    assert not deployment_fleet.direct_host_writes_allowed(settings)
    for service in (
        deployment_fleet.install_generated_key,
        deployment_fleet.provision_host,
        deployment_fleet.retire_host,
    ):
        with pytest.raises(PermissionError, match="BOSS_SSH_HOST_DEPLOY_ALLOWED"):
            service(object(), settings, host_id="dh_nonexistent")


@pytest.mark.parametrize("edition", ["community", "oss", "local"])
def test_community_fleet_routes_have_zero_service_or_host_effects(
    client, engine, monkeypatch, edition,
):
    host_id = _host(client, uuid4().hex[:10])
    monkeypatch.setattr(api_main.settings, "boss_edition", edition)
    monkeypatch.setattr(api_main.settings, "ssh_host_deploy_allowed", True)
    effects = []
    for method in ("install_generated_key", "provision_host", "retire_host"):
        monkeypatch.setattr(
            deployment_fleet, method,
            lambda *_args, _method=method, **_kwargs: effects.append(_method),
        )
    with engine.begin() as connection:
        before_host = connection.execute(
            text("SELECT status, key_secret_ref, key_public FROM deployment_hosts WHERE id=:i"),
            {"i": host_id},
        ).one()
        before_events = connection.execute(
            text("SELECT count(*) FROM events WHERE project_id=:p"), {"p": HOME},
        ).scalar_one()
    for action in WRITE_ACTIONS:
        denied = client.post(
            f"/v1/deployment-hosts/{host_id}/{action}", json={"actor": "ceo"},
        )
        assert denied.status_code == 409, (action, denied.text)
        assert "Direct deployment-host changes" in denied.text
    with engine.begin() as connection:
        after_host = connection.execute(
            text("SELECT status, key_secret_ref, key_public FROM deployment_hosts WHERE id=:i"),
            {"i": host_id},
        ).one()
        after_events = connection.execute(
            text("SELECT count(*) FROM events WHERE project_id=:p"), {"p": HOME},
        ).scalar_one()
    assert effects == []
    assert after_host == before_host
    assert after_events == before_events
    listing = client.get(f"/v1/deployment-hosts?project_id={HOME}")
    assert listing.status_code == 200
    assert listing.json()["preconditions"]["host_writes_allowed"] is False


def test_fleet_writes_require_stored_project_provider_permission(
    client, engine, monkeypatch,
):
    host_id = _host(client, uuid4().hex[:10])
    suffix = uuid4().hex[:10]
    operator = f"fleet_operator_{suffix}"
    reader = f"fleet_reader_{suffix}"
    key = f"fleet-key-{suffix}"
    monkeypatch.setattr(api_main.settings, "boss_edition", "enterprise_self_hosted")
    monkeypatch.setattr(api_main.settings, "ssh_host_deploy_allowed", True)
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", key)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{key}:{reader}")
    with engine.begin() as connection:
        api_main.members.upsert_member(
            connection, project_id=HOME, member_id=reader,
            display_name=reader, member_type="human", role_keys=[],
            permissions=["task_create"], actor="ceo",
        )
        workspace_id = connection.execute(
            text("SELECT workspace_id FROM projects WHERE id=:p"), {"p": HOME},
        ).scalar_one()
        foreign = f"fleet_foreign_{suffix}"
        connection.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) "
                 "VALUES (:i, :w, 'Foreign fleet', :i)"),
            {"i": foreign, "w": workspace_id},
        )
        connection.execute(
            text("INSERT INTO deployment_hosts "
                 "(id, project_id, name, address, ssh_user, ssh_port, status) "
                 "VALUES (:i, :p, 'foreign', '203.0.113.20', 'root', 22, 'registered')"),
            {"i": f"dh_foreign_{suffix}", "p": foreign},
        )
        foreign_host_id = f"dh_foreign_{suffix}"
        api_main.members.upsert_member(
            connection, project_id=HOME, member_id=operator,
            display_name=operator, member_type="human", role_keys=[],
            permissions=["providerops_manage"], actor="ceo",
        )
    effects = []
    for method in ("install_generated_key", "provision_host", "retire_host"):
        monkeypatch.setattr(
            deployment_fleet, method,
            lambda *_args, _method=method, **_kwargs: effects.append(_method),
        )
    headers = {"Authorization": f"Bearer {key}"}
    for action in WRITE_ACTIONS:
        denied = client.post(
            f"/v1/deployment-hosts/{host_id}/{action}",
            headers=headers, json={"actor": reader},
        )
        assert denied.status_code == 403, (action, denied.text)

    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{key}:{operator}")
    hidden = client.get(
        f"/v1/deployment-hosts?project_id={foreign}", headers=headers,
    )
    assert hidden.status_code == 403, hidden.text
    for action in WRITE_ACTIONS:
        denied = client.post(
            f"/v1/deployment-hosts/{foreign_host_id}/{action}",
            headers=headers, json={"actor": operator},
        )
        assert denied.status_code == 403, (action, denied.text)
    assert effects == []

    for action in WRITE_ACTIONS:
        allowed = client.post(
            f"/v1/deployment-hosts/{host_id}/{action}",
            headers=headers, json={"actor": operator},
        )
        assert allowed.status_code == 200, (action, allowed.text)
    assert effects == ["install_generated_key", "provision_host", "retire_host"]
