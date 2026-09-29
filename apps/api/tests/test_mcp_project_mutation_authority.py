"""Project metadata writes cannot borrow a CEO identity from an MCP request."""

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.mcp.api_surface import ApiSurface, operation_alias


PROJECT = "boss-dogfood"


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_revoked_ceo_key_cannot_restore_membership_or_change_autonomy(
    client, engine, monkeypatch,
):
    ceo_key = f"revoked-ceo-{uuid4().hex[:10]}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", ceo_key)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{ceo_key}:ceo")
    with engine.begin() as connection:
        # Seeded CEOs also have an org mirror. A project-level revocation must
        # override that inherited membership and must not trigger re-seeding.
        assert connection.execute(
            text("SELECT status FROM org_members WHERE member_id = 'ceo' LIMIT 1")
        ).scalar_one() == "active"
        connection.execute(
            text("UPDATE project_members SET status = 'inactive' "
                 "WHERE project_id = :project_id AND member_id = 'ceo'"),
            {"project_id": PROJECT},
        )
        api_main.members.seed_default_members(connection, PROJECT, actor="bootstrap")

    body = {"project_id": PROJECT, "actor": "ceo", "mode": "full_auto"}
    assert client.post("/v1/console/v2/autonomy", headers=_headers(ceo_key),
                       json=body).status_code == 403
    assert client.post("/v1/console/v2/autonomy/apply", headers=_headers(ceo_key),
                       json={"project_id": PROJECT}).status_code == 403
    project_list = client.get("/v1/projects", headers=_headers(ceo_key))
    assert project_list.status_code == 200
    assert PROJECT not in {item["id"] for item in project_list.json()["items"]}
    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT status FROM project_members "
                 "WHERE project_id = :project_id AND member_id = 'ceo'"),
            {"project_id": PROJECT},
        ).scalar_one() == "inactive"
        assert "ceo" not in {
            row["member_id"] for row in api_main.members.list_members(connection, PROJECT)
        }
        assert connection.execute(
            text("SELECT count(*) FROM events WHERE project_id = :project_id "
                 "AND event_type IN ('autonomy.policy_set', 'autonomy.policy_applied')"),
            {"project_id": PROJECT},
        ).scalar_one() == 0


def test_permission_removed_from_ceo_is_not_restored_by_authorization(
    client, engine, monkeypatch,
):
    ceo_key = f"limited-ceo-{uuid4().hex[:10]}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", ceo_key)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{ceo_key}:ceo")
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE project_members SET permissions = permissions - 'decision_resolve' "
                 "WHERE project_id = :project_id AND member_id = 'ceo'"),
            {"project_id": PROJECT},
        )
        api_main.members.seed_default_members(connection, PROJECT, actor="bootstrap")

    assert client.post(
        "/v1/console/v2/autonomy", headers=_headers(ceo_key),
        json={"project_id": PROJECT, "mode": "full_auto"},
    ).status_code == 403
    with engine.begin() as connection:
        permissions = connection.execute(
            text("SELECT permissions FROM project_members "
                 "WHERE project_id = :project_id AND member_id = 'ceo'"),
            {"project_id": PROJECT},
        ).scalar_one()
    assert "decision_resolve" not in permissions


def test_project_metadata_writes_bind_permission_and_record_real_actor(
    client, engine, monkeypatch,
):
    suffix = uuid4().hex[:10]
    mcp_key, editor_key, ceo_key = (
        f"mcp-{suffix}", f"editor-{suffix}", f"ceo-{suffix}"
    )
    mcp_member, editor = f"mcp_{suffix}", f"editor_{suffix}"
    role_key, meeting_key = f"new_role_{suffix}", f"meeting_{suffix}"
    other_project = f"other_{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(
        api_main.settings, "api_keys", f"{mcp_key},{editor_key},{ceo_key}"
    )
    monkeypatch.setattr(
        api_main.settings, "api_key_principals",
        f"{mcp_key}:{mcp_member},{editor_key}:{editor},{ceo_key}:ceo",
    )
    with engine.begin() as connection:
        # Even a model principal granted a CEO role and decision_resolve is not
        # the human CEO who may change the policy that resolves later gates.
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id=mcp_member,
            display_name="MCP agent", member_type="agent", role_keys=["ceo"],
            permissions=["decision_resolve"], actor="ceo",
        )
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id=editor,
            display_name="Project editor", member_type="agent", role_keys=[],
            permissions=["role_review", "workflow_manage"], actor="ceo",
        )
        workspace_id = connection.execute(
            text("SELECT workspace_id FROM projects WHERE id = :project_id"),
            {"project_id": PROJECT},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) "
                 "VALUES (:id, :workspace, 'Other project', :id)"),
            {"id": other_project, "workspace": workspace_id},
        )

    role_body = {"project_id": PROJECT, "role_key": role_key}
    meeting_body = {
        "project_id": PROJECT, "actor": "ceo", "key": meeting_key,
        "title": "Review", "lens": "Review the plan",
    }
    org_body = {
        "project_id": PROJECT, "actor": "ceo", "reports_to": "cto",
    }
    autonomy_body = {
        "project_id": PROJECT, "actor": "ceo", "mode": "full_auto",
        "threshold_usd": 25,
    }

    # The dedicated MCP member cannot claim CEO in a direct API request.
    assert client.post("/v1/meeting-roles", headers=_headers(mcp_key),
                       json=meeting_body).status_code == 403
    assert client.put("/v1/console/v2/org/qa", headers=_headers(mcp_key),
                      json=org_body).status_code == 403
    # Role registration and autonomy controls are held for nonhuman callers
    # before the handler can inspect their project permissions.
    assert client.post("/v1/roles", headers=_headers(mcp_key),
                       json=role_body).status_code == 409
    assert client.post("/v1/console/v2/autonomy", headers=_headers(mcp_key),
                       json=autonomy_body).status_code == 409
    assert client.post("/v1/console/v2/autonomy/apply", headers=_headers(mcp_key),
                       json={"project_id": PROJECT, "actor": "ceo"}).status_code == 409

    # The same calls through the API-backed MCP bridge retain that authority.
    with TestClient(api_main.app) as mcp_client:
        mcp_client.headers.update(_headers(mcp_key))
        surface = ApiSurface.connect(
            "http://127.0.0.1:8000", api_key=mcp_key,
            project_id=PROJECT, client=mcp_client,
        )
        for method, path, path_args, query, body in [
            ("POST", "/v1/meeting-roles", {}, {},
             {k: v for k, v in meeting_body.items() if k != "actor"}),
            ("PUT", "/v1/console/v2/org/{agent_id}", {"agent_id": "qa"}, {},
             {k: v for k, v in org_body.items() if k != "actor"}),
        ]:
            result = surface.invoke(
                operation_alias(method, path), project_id=PROJECT,
                path=path_args, query=query, body=body,
            )
            assert result["status"] == 403, result
        for path in (
            "/v1/console/v2/autonomy", "/v1/console/v2/autonomy/apply",
        ):
            result = surface.invoke(
                operation_alias("POST", path), project_id=PROJECT,
                body={"mode": "full_auto"} if path.endswith("autonomy") else {},
            )
            assert result["error"] == "operator_only_unavailable", result

    # A project editor may use those safe metadata routes; attribution comes
    # from the authenticated principal, even when the body claims CEO.
    with TestClient(api_main.app) as editor_client:
        editor_client.headers.update(_headers(editor_key))
        surface = ApiSurface.connect(
            "http://127.0.0.1:8000", api_key=editor_key,
            project_id=PROJECT, client=editor_client,
        )
        result = surface.invoke(
            operation_alias("POST", "/v1/meeting-roles"),
            project_id=PROJECT,
            body={k: v for k, v in meeting_body.items() if k != "actor"},
        )
        assert result["status"] == 201, result
        result = surface.invoke(
            operation_alias("PUT", "/v1/console/v2/org/{agent_id}"),
            project_id=PROJECT, path={"agent_id": "qa"},
            body={k: v for k, v in org_body.items() if k != "actor"},
        )
        assert result["status"] == 200, result
    assert client.post("/v1/roles", headers=_headers(editor_key),
                       json=role_body).status_code == 409
    assert client.patch(f"/v1/meeting-roles/{meeting_key}",
                        headers=_headers(mcp_key),
                        params={"project_id": PROJECT, "enabled": False,
                                "actor": "ceo"}).status_code == 403
    assert client.delete(f"/v1/meeting-roles/{meeting_key}",
                         headers=_headers(mcp_key),
                         params={"project_id": PROJECT, "actor": "ceo"}).status_code == 409
    assert client.delete("/v1/console/v2/org/qa", headers=_headers(mcp_key),
                         params={"project_id": PROJECT, "actor": "ceo"}).status_code == 409
    with engine.begin() as connection:
        assert connection.execute(text("""
            SELECT count(*) FROM meeting_roles
            WHERE project_id = :project_id AND role_key = :role_key
        """), {"project_id": PROJECT, "role_key": meeting_key}).scalar_one() == 1
        assert connection.execute(text("""
            SELECT count(*) FROM org_assignments
            WHERE project_id = :project_id AND agent_id = 'qa'
        """), {"project_id": PROJECT}).scalar_one() == 1
        assert connection.execute(text("""
            SELECT count(*) FROM roles
            WHERE project_id = :project_id AND role_key = :role_key
        """), {"project_id": PROJECT, "role_key": role_key}).scalar_one() == 0

    # A principal may not reuse its first project's authority in a sibling.
    assert client.post("/v1/meeting-roles", headers=_headers(editor_key),
                       json={**meeting_body, "project_id": other_project}).status_code == 403
    assert client.put("/v1/console/v2/org/qa", headers=_headers(editor_key),
                      json={**org_body, "project_id": other_project}).status_code == 403
    assert client.post("/v1/roles", headers=_headers(editor_key),
                       json={**role_body, "project_id": other_project}).status_code == 409

    # The human CEO retains the controls, and every successful write is audited.
    assert client.post("/v1/console/v2/autonomy", headers=_headers(ceo_key),
                       json=autonomy_body).status_code == 200
    assert client.post("/v1/console/v2/autonomy/apply", headers=_headers(ceo_key),
                       json={"project_id": PROJECT, "actor": "ceo"}).status_code == 200
    assert client.post("/v1/roles", headers=_headers(ceo_key),
                       json=role_body).status_code == 201
    assert client.patch(f"/v1/meeting-roles/{meeting_key}",
                        headers=_headers(editor_key),
                        params={"project_id": PROJECT, "enabled": False}).status_code == 200
    assert client.delete("/v1/console/v2/org/qa", headers=_headers(editor_key),
                         params={"project_id": PROJECT}).status_code == 409
    assert client.delete(f"/v1/meeting-roles/{meeting_key}",
                         headers=_headers(editor_key),
                         params={"project_id": PROJECT}).status_code == 409
    assert client.delete("/v1/console/v2/org/qa", headers=_headers(ceo_key),
                         params={"project_id": PROJECT}).status_code == 200
    assert client.delete(f"/v1/meeting-roles/{meeting_key}",
                         headers=_headers(ceo_key),
                         params={"project_id": PROJECT}).status_code == 204
    with engine.begin() as connection:
        rows = connection.execute(
            text("SELECT event_type, actor FROM events WHERE project_id = :project_id "
                 "AND event_type IN ('meeting_role.upserted', 'meeting_role.enabled_changed', "
                 "'meeting_role.deleted', 'role.registered', 'org.assignment_set', "
                 "'org.assignment_cleared', 'autonomy.policy_set', "
                 "'autonomy.policy_applied')"),
            {"project_id": PROJECT},
        ).mappings().all()
    actors = {row["event_type"]: row["actor"] for row in rows}
    assert actors == {
        "meeting_role.upserted": editor,
        "meeting_role.enabled_changed": editor,
        "meeting_role.deleted": "ceo",
        "role.registered": "ceo",
        "org.assignment_set": editor,
        "org.assignment_cleared": "ceo",
        "autonomy.policy_set": "ceo",
        "autonomy.policy_applied": "ceo",
    }
