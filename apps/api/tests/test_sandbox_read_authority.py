"""A sandbox ID cannot expose another project's host files or logs."""

from uuid import uuid4

from sqlalchemy import text

from apps.api.app import main as api_main


def test_sandbox_reads_resolve_stored_project_before_host_access(client, engine, monkeypatch):
    suffix = uuid4().hex[:10]
    home = "boss-dogfood"
    foreign = f"sandbox_foreign_{suffix}"
    reader = f"sandbox_reader_{suffix}"
    key = f"sandbox-key-{suffix}"
    home_sandbox = f"sandbox_home_{suffix}"
    foreign_sandbox = f"sandbox_other_{suffix}"
    home_run = f"run_home_{suffix}"
    foreign_run = f"run_foreign_{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", key)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{key}:{reader}")
    monkeypatch.setattr(api_main.settings, "sandbox_agent_url", "")
    headers = {"Authorization": f"Bearer {key}"}

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
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM code_task_runs WHERE id IN (:home, :foreign)"),
                {"home": home_run, "foreign": foreign_run},
            )
            connection.execute(
                text("DELETE FROM project_members WHERE member_id = :reader"),
                {"reader": reader},
            )
            connection.execute(
                text("DELETE FROM events WHERE project_id = :id"), {"id": foreign},
            )
            connection.execute(
                text("DELETE FROM projects WHERE id = :id"), {"id": foreign},
            )
