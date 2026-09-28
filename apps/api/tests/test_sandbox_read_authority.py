"""Sandbox IDs resolve to stored projects before reads or host effects."""

from uuid import uuid4

from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.services import sandbox_console


def test_sandbox_routes_resolve_stored_project_before_host_access(client, engine, monkeypatch):
    suffix = uuid4().hex[:10]
    home = "boss-dogfood"
    foreign = f"sandbox_foreign_{suffix}"
    reader = f"sandbox_reader_{suffix}"
    manager = f"sandbox_manager_{suffix}"
    key = f"sandbox-key-{suffix}"
    manager_key = f"sandbox-manager-key-{suffix}"
    home_sandbox = f"sandbox_home_{suffix}"
    foreign_sandbox = f"sandbox_other_{suffix}"
    home_run = f"run_home_{suffix}"
    foreign_run = f"run_foreign_{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", f"{key},{manager_key}")
    monkeypatch.setattr(
        api_main.settings, "api_key_principals",
        f"{key}:{reader},{manager_key}:{manager}",
    )
    monkeypatch.setattr(api_main.settings, "sandbox_agent_url", "")
    headers = {"Authorization": f"Bearer {key}"}
    manager_headers = {"Authorization": f"Bearer {manager_key}"}
    host_calls = []

    def fake_exec(*args, **kwargs):
        host_calls.append(("exec", args[2], kwargs.get("actor")))
        return {"blocked": True}

    def fake_hold(*args, **kwargs):
        host_calls.append(("hold", args[2], kwargs.get("actor")))
        return {"held": True}

    def fake_destroy(*args, **kwargs):
        host_calls.append(("destroy", args[1], None))
        return {"destroyed": True}

    monkeypatch.setattr(sandbox_console, "exec_command", fake_exec)
    monkeypatch.setattr(sandbox_console, "hold", fake_hold)
    monkeypatch.setattr(sandbox_console, "destroy", fake_destroy)

    with engine.begin() as connection:
        workspace = connection.execute(
            text("SELECT workspace_id FROM projects WHERE id = :id"), {"id": home},
        ).scalar_one()
        connection.execute(
            text("""INSERT INTO projects (id, workspace_id, name, slug)
                    VALUES (:id, :workspace_id, 'Foreign sandbox', :id)"""),
            {"id": foreign, "workspace_id": workspace},
        )
        api_main.members.upsert_member(
            connection, project_id=home, member_id=reader,
            display_name="Sandbox reader", member_type="human", role_keys=[],
            permissions=[], actor="ceo",
        )
        api_main.members.upsert_member(
            connection, project_id=home, member_id=manager,
            display_name="Sandbox manager", member_type="human", role_keys=[],
            permissions=["providerops_manage"], actor="ceo",
        )
        for project_id, run_id, sandbox_id in (
            (home, home_run, home_sandbox),
            (foreign, foreign_run, foreign_sandbox),
        ):
            connection.execute(
                text("""INSERT INTO code_task_runs
                        (id, project_id, title, status, finalize_ctx)
                        VALUES (:id, :project_id, :title, 'failed',
                                CAST(:finalize_ctx AS JSONB))"""),
                {"id": run_id, "project_id": project_id,
                 "title": f"{project_id} private run",
                 "finalize_ctx": '{"sandbox_task_id":"' + sandbox_id + '"}'},
            )

    try:
        listing = client.get("/v1/sandboxes", headers=headers)
        assert listing.status_code == 200, listing.text
        ids = {item["sandbox_id"] for item in listing.json()["sandboxes"]}
        assert home_sandbox in ids
        assert foreign_sandbox not in ids

        paths = (
            f"/v1/sandboxes/{foreign_sandbox}/tree",
            f"/v1/sandboxes/{foreign_sandbox}/file?path=notes.txt",
            f"/v1/sandboxes/{foreign_sandbox}/stats",
            f"/v1/sandboxes/{foreign_sandbox}/logs",
        )
        for path in paths:
            response = client.get(path, headers=headers)
            assert response.status_code == 403, (path, response.text)
            assert foreign not in response.text
        unknown = client.get(f"/v1/sandboxes/unknown_{suffix}/tree", headers=headers)
        assert unknown.status_code == 404, unknown.text

        for sandbox_id, caller_headers in (
            (home_sandbox, headers),
            (foreign_sandbox, headers),
            (foreign_sandbox, manager_headers),
        ):
            exec_response = client.post(
                f"/v1/sandboxes/{sandbox_id}/exec", headers=caller_headers,
                json={"command": "true", "actor": manager},
            )
            hold_response = client.post(
                f"/v1/sandboxes/{sandbox_id}/preserve", headers=caller_headers,
                json={"hours": 2, "actor": manager},
            )
            delete_response = client.delete(
                f"/v1/sandboxes/{sandbox_id}", headers=caller_headers,
            )
            assert [r.status_code for r in (exec_response, hold_response, delete_response)] == [
                403, 403, 403,
            ]
        assert host_calls == []

        unknown_exec = client.post(
            f"/v1/sandboxes/unknown_{suffix}/exec", headers=manager_headers,
            json={"command": "true"},
        )
        assert unknown_exec.status_code == 404
        assert host_calls == []

        allowed_exec = client.post(
            f"/v1/sandboxes/{home_sandbox}/exec", headers=manager_headers,
            json={"command": "true", "actor": reader},
        )
        assert allowed_exec.status_code == 200, allowed_exec.text
        assert host_calls == [("exec", home_sandbox, manager)]

        hold = client.post(
            f"/v1/sandboxes/{home_sandbox}/preserve", headers=manager_headers,
            json={"hours": 2},
        )
        delete = client.delete(
            f"/v1/sandboxes/{home_sandbox}", headers=manager_headers,
        )
        assert hold.status_code == delete.status_code == 409
        assert "receipt" in hold.text.lower()
        assert "rollback" in delete.text.lower()
        assert host_calls == [("exec", home_sandbox, manager)]
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM code_task_runs WHERE id IN (:home, :foreign)"),
                {"home": home_run, "foreign": foreign_run},
            )
            connection.execute(
                text("DELETE FROM project_members WHERE member_id IN (:reader, :manager)"),
                {"reader": reader, "manager": manager},
            )
            connection.execute(
                text("DELETE FROM events WHERE project_id = :id"), {"id": foreign},
            )
            connection.execute(
                text("DELETE FROM projects WHERE id = :id"), {"id": foreign},
            )
