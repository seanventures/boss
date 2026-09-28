"""Deployment target writes must use stored project authority before any host effect."""

from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.services import delivery_pipeline, deploy_service, deployment_connectors


HOME = "boss-dogfood"


@pytest.mark.parametrize("edition", ["community", "oss", "local"])
def test_community_ssh_deploy_never_bootstraps_or_writes_host(
    engine, monkeypatch, edition,
):
    connector = deployment_connectors.SSHContainerConnector()
    effects = []
    monkeypatch.setattr(api_main.settings, "boss_edition", edition)
    monkeypatch.setattr(
        connector, "validate", lambda *_args: effects.append("validate") or []
    )
    monkeypatch.setattr(
        connector, "bootstrap",
        lambda *_args: effects.append("host_write") or {"suggested_port": 3010},
    )
    monkeypatch.setattr(
        deploy_service, "deploy_ssh", lambda *_args, **_kwargs:
        effects.append("deploy_ssh") or {"status": "deployed"},
    )
    target = {"id": "deptgt_unconfigured", "required_resources": [],
              "target_ref": "root@203.0.113.10", "repo": "example"}
    assert connector._ensure_port(engine, api_main.settings, target) == target
    for candidate in (
        target,
        {**target, "required_resources": [
            {"kind": "app_port", "name": "3010"}
        ]},
    ):
        result = connector.apply(
            engine, api_main.settings, candidate, "crun_unconfigured", {}
        )
        assert result["status"] == "blocked"
        assert "Community SSH deployment" in result["problems"][0]
    assert effects == []


@pytest.mark.parametrize("edition", ["community", "oss", "local"])
def test_community_ssh_plan_records_blocked_decision_before_build(
    client, engine, monkeypatch, edition,
):
    suffix = uuid4().hex[:10]
    key = f"deploy-plan-key-{suffix}"
    target_id = f"deptgt_plan_{suffix}"
    monkeypatch.setattr(api_main.settings, "boss_edition", edition)
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", key)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{key}:ceo")
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO deployment_targets "
                 "(id, project_id, app_name, repo, environment, target_kind, "
                 "target_ref, status, required_resources, created_by) VALUES "
                 "(:id, :project_id, :app_name, 'example', 'staging', 'ssh', "
                 "'root@203.0.113.17', 'active', CAST(:resources AS JSONB), 'ceo')"),
            {"id": target_id, "project_id": HOME,
             "app_name": f"plan-{suffix}",
             "resources": '[{"kind":"app_port","name":"3010"}]'},
        )
    effects = []
    monkeypatch.setattr(
        api_main, "_start_deployment_in_background",
        lambda *_args, **_kwargs: effects.append("background") or {},
    )
    monkeypatch.setattr(
        deployment_connectors, "connector_for",
        lambda *_args, **_kwargs: effects.append("connector") or None,
    )
    denied = client.post(
        "/v1/delivery/deploy", headers={"Authorization": f"Bearer {key}"},
        json={"target_id": target_id, "actor": "ceo", "ref": "main"},
    )
    assert denied.status_code == 409, denied.text
    assert "Community host deployment" in denied.text
    with engine.begin() as connection:
        run = connection.execute(
            text("SELECT id, status, decision_id, changeset_id, failure_reason "
                 "FROM deployment_runs WHERE deployment_target_id=:id"),
            {"id": target_id},
        ).mappings().one()
    assert run["status"] == "failed"
    assert run["decision_id"]
    assert run["changeset_id"] is None
    assert "Community host deployment" in run["failure_reason"]
    replay = delivery_pipeline.execute_deployment(
        engine, api_main.settings, run["id"], actor="ceo"
    )
    assert replay["status"] == "blocked"
    assert effects == []


def test_target_mutations_require_project_actor(client, engine, monkeypatch):
    suffix = uuid4().hex[:10]
    foreign = f"deploy_foreign_{suffix}"
    operator = f"deploy_operator_{suffix}"
    reader = f"deploy_reader_{suffix}"
    operator_key = f"deploy-operator-key-{suffix}"
    reader_key = f"deploy-reader-key-{suffix}"
    operator_headers = {"Authorization": f"Bearer {operator_key}"}
    reader_headers = {"Authorization": f"Bearer {reader_key}"}
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", f"{operator_key},{reader_key}")
    monkeypatch.setattr(
        api_main.settings, "api_key_principals",
        f"{operator_key}:{operator},{reader_key}:{reader}",
    )

    with engine.begin() as connection:
        workspace_id = connection.execute(
            text("SELECT workspace_id FROM projects WHERE id=:id"), {"id": HOME}
        ).scalar_one()
        connection.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) "
                 "VALUES (:id, :workspace_id, 'Foreign deployment', :id)"),
            {"id": foreign, "workspace_id": workspace_id},
        )
        for member_id, permissions in (
            (operator, ["providerops_manage"]), (reader, []),
        ):
            api_main.members.upsert_member(
                connection, project_id=HOME, member_id=member_id,
                display_name=member_id, member_type="human", role_keys=[],
                permissions=permissions, actor="ceo",
            )

    body = {
        "app_name": f"ga-deployment-{suffix}",
        "repo": "example", "environment": "staging", "target_kind": "ssh",
        "target_ref": "root@203.0.113.12", "actor": operator,
    }
    create_path = "/v1/deployment-targets"
    assert client.post(
        create_path, headers=operator_headers,
        json={**body, "project_id": foreign},
    ).status_code == 403
    assert client.post(
        create_path, headers=reader_headers,
        json={**body, "project_id": HOME, "actor": reader},
    ).status_code == 403
    assert client.post(
        create_path, headers=operator_headers,
        json={**body, "project_id": HOME, "actor": "ceo"},
    ).status_code == 403
    assert client.post(
        create_path, headers=operator_headers,
        json={**body, "project_id": HOME, "actor": ""},
    ).status_code == 422

    created = client.post(
        create_path, headers=operator_headers,
        json={**body, "project_id": HOME},
    )
    assert created.status_code == 201, created.text
    target_id = created.json()["id"]
    foreign_target = f"deptgt_foreign_{suffix}"
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO deployment_targets "
                 "(id, project_id, app_name, repo, environment, target_kind, "
                 "target_ref, status, created_by) VALUES "
                 "(:id, :project_id, :app_name, 'example', 'staging', 'ssh', "
                 "'root@203.0.113.13', 'retired', 'test')"),
            {"id": foreign_target, "project_id": foreign,
             "app_name": f"foreign-{suffix}"},
        )

    for candidate in (target_id, foreign_target):
        path = f"/v1/deployment-targets/{candidate}"
        denied = client.patch(
            path, headers=reader_headers,
            json={"actor": reader, "target_ref": "root@203.0.113.99"},
        )
        assert denied.status_code == 403, denied.text
        denied = client.post(
            f"{path}/auto-deploy", headers=reader_headers,
            json={"actor": reader, "auto_deploy": True},
        )
        assert denied.status_code == 403, denied.text
        if candidate == foreign_target:
            denied = client.patch(
                path, headers=operator_headers,
                json={"actor": operator, "target_ref": "root@203.0.113.99"},
            )
            assert denied.status_code == 403, denied.text
            denied = client.post(
                f"{path}/auto-deploy", headers=operator_headers,
                json={"actor": operator, "paused": True},
            )
            assert denied.status_code == 403, denied.text

    path = f"/v1/deployment-targets/{target_id}"
    assert client.patch(
        path, headers=operator_headers, json={"target_ref": "root@203.0.113.14"}
    ).status_code == 422
    assert client.post(
        f"{path}/auto-deploy", headers=operator_headers,
        json={"auto_deploy": True},
    ).status_code == 422
    assert client.patch(
        path, headers=operator_headers,
        json={"actor": operator, "target_ref": "root@203.0.113.14"},
    ).status_code == 200
    assert client.post(
        f"{path}/auto-deploy", headers=operator_headers,
        json={"actor": operator, "auto_deploy": True},
    ).status_code == 200
    with engine.begin() as connection:
        home_row = connection.execute(
            text("SELECT target_ref, auto_deploy FROM deployment_targets WHERE id=:id"),
            {"id": target_id},
        ).mappings().one()
        foreign_row = connection.execute(
            text("SELECT target_ref, auto_deploy, paused FROM deployment_targets "
                 "WHERE id=:id"), {"id": foreign_target},
        ).mappings().one()
    assert home_row["target_ref"] == "root@203.0.113.14"
    assert home_row["auto_deploy"] is True
    assert foreign_row["target_ref"] == "root@203.0.113.13"
    assert foreign_row["auto_deploy"] is False
    assert foreign_row["paused"] is False


def test_direct_host_and_history_effects_fail_closed(
    client, engine, monkeypatch,
):
    suffix = uuid4().hex[:10]
    operator = f"deploy_gate_operator_{suffix}"
    reader = f"deploy_gate_reader_{suffix}"
    operator_key = f"deploy-gate-key-{suffix}"
    reader_key = f"deploy-gate-reader-key-{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", f"{operator_key},{reader_key}")
    monkeypatch.setattr(
        api_main.settings, "api_key_principals",
        f"{operator_key}:{operator},{reader_key}:{reader}",
    )
    operator_headers = {"Authorization": f"Bearer {operator_key}"}
    reader_headers = {"Authorization": f"Bearer {reader_key}"}
    target_id = f"deptgt_gate_{suffix}"
    foreign = f"deploy_gate_foreign_{suffix}"
    foreign_target = f"deptgt_gate_foreign_{suffix}"
    with engine.begin() as connection:
        workspace_id = connection.execute(
            text("SELECT workspace_id FROM projects WHERE id=:id"), {"id": HOME}
        ).scalar_one()
        connection.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) "
                 "VALUES (:id, :workspace_id, 'Foreign host gate', :id)"),
            {"id": foreign, "workspace_id": workspace_id},
        )
        api_main.members.upsert_member(
            connection, project_id=HOME, member_id=operator,
            display_name=operator, member_type="human", role_keys=[],
            permissions=["providerops_manage"], actor="ceo",
        )
        api_main.members.upsert_member(
            connection, project_id=HOME, member_id=reader,
            display_name=reader, member_type="human", role_keys=[],
            permissions=[], actor="ceo",
        )
        connection.execute(
            text("INSERT INTO deployment_targets "
                 "(id, project_id, app_name, repo, environment, target_kind, "
                 "target_ref, status, created_by) VALUES "
                 "(:id, :project_id, :app_name, 'example', 'staging', 'ssh', "
                 "'root@203.0.113.15', 'retired', 'test')"),
            {"id": target_id, "project_id": HOME,
             "app_name": f"gate-{suffix}"},
        )
        connection.execute(
            text("INSERT INTO deployment_targets "
                 "(id, project_id, app_name, repo, environment, target_kind, "
                 "target_ref, status, created_by) VALUES "
                 "(:id, :project_id, :app_name, 'example', 'staging', 'ssh', "
                 "'root@203.0.113.16', 'retired', 'test')"),
            {"id": foreign_target, "project_id": foreign,
             "app_name": f"foreign-gate-{suffix}"},
        )

    effects = []
    monkeypatch.setattr(
        deployment_connectors, "connector_for",
        lambda *_args, **_kwargs: effects.append("connector") or None,
    )
    path = f"/v1/deployment-targets/{target_id}"
    cases = (
        (f"{path}/bootstrap", {"actor": operator}),
        (f"{path}/teardown", {"actor": operator, "stop_only": True}),
        (f"{path}/teardown", {"actor": operator,
                              "confirm": f"gate-{suffix}", "include_data": True}),
        (f"{path}/purge", {"actor": operator, "confirm": f"gate-{suffix}"}),
    )
    for route, payload in cases:
        assert client.post(
            route, headers=operator_headers,
            json={**payload, "actor": ""},
        ).status_code == 422
        denied = client.post(
            route, headers=reader_headers, json={**payload, "actor": reader}
        )
        assert denied.status_code == 403, (route, denied.text)
        foreign_route = route.replace(target_id, foreign_target)
        denied_foreign = client.post(
            foreign_route, headers=operator_headers, json=payload
        )
        assert denied_foreign.status_code == 403, (foreign_route, denied_foreign.text)
        blocked = client.post(route, headers=operator_headers, json=payload)
        assert blocked.status_code == 409, (route, blocked.text)
        assert "disabled" in blocked.text.lower()
    assert effects == []
    with engine.begin() as connection:
        status = connection.execute(
            text("SELECT status FROM deployment_targets WHERE id=:id"),
            {"id": target_id},
        ).scalar_one()
    assert status == "retired"
