"""Object IDs must not let an authenticated member read a foreign project."""

from uuid import uuid4

from sqlalchemy import text

from apps.api.app import main as api_main


def test_core_detail_reads_require_stored_project_membership(client, engine, monkeypatch):
    suffix = uuid4().hex[:10]
    reader = f"detail_reader_{suffix}"
    key = f"detail-key-{suffix}"
    home = "boss-dogfood"
    foreign = f"detail_foreign_{suffix}"
    ids = {kind: (f"{kind}_home_{suffix}", f"{kind}_foreign_{suffix}")
           for kind in ("task", "job", "artifact", "receipt")}
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", key)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{key}:{reader}")
    headers = {"Authorization": f"Bearer {key}"}

    with engine.begin() as connection:
        scope = connection.execute(
            text("""SELECT p.workspace_id, w.org_id FROM projects p
                    JOIN workspaces w ON w.id = p.workspace_id WHERE p.id = :id"""),
            {"id": home},
        ).mappings().one()
        connection.execute(
            text("""INSERT INTO projects (id, workspace_id, name, slug)
                    VALUES (:id, :workspace_id, 'Foreign detail test', :id)"""),
            {"id": foreign, "workspace_id": scope["workspace_id"]},
        )
        api_main.members.upsert_member(
            connection, project_id=home, member_id=reader,
            display_name="Detail reader", member_type="human", role_keys=[],
            permissions=[], actor="ceo",
        )
        for project_id, position in ((home, 0), (foreign, 1)):
            task_id = ids["task"][position]
            job_id = ids["job"][position]
            artifact_id = ids["artifact"][position]
            receipt_id = ids["receipt"][position]
            connection.execute(
                text("""INSERT INTO tasks (id, project_id, title, dedupe_key)
                        VALUES (:id, :project_id, :title, :id)"""),
                {"id": task_id, "project_id": project_id,
                 "title": f"{project_id} private task"},
            )
            connection.execute(
                text("""INSERT INTO jobs (id, project_id, task_id, kind)
                        VALUES (:id, :project_id, :task_id, 'noop')"""),
                {"id": job_id, "project_id": project_id, "task_id": task_id},
            )
            connection.execute(
                text("""INSERT INTO artifacts
                        (id, project_id, task_id, artifact_type, title, content)
                        VALUES (:id, :project_id, :task_id, 'note', :title,
                                CAST(:content AS JSONB))"""),
                {"id": artifact_id, "project_id": project_id, "task_id": task_id,
                 "title": f"{project_id} private artifact",
                 "content": '{"markdown":"# Private artifact"}'},
            )
            connection.execute(
                text("""INSERT INTO receipts
                        (id, org_id, workspace_id, project_id, dedupe_key,
                         effect_kind, payload_hash)
                        VALUES (:id, :org_id, :workspace_id, :project_id,
                                :id, 'test.read', :payload_hash)"""),
                {"id": receipt_id, "org_id": scope["org_id"],
                 "workspace_id": scope["workspace_id"],
                 "project_id": project_id, "payload_hash": "0" * 64},
            )

    try:
        paths = (
            lambda i: f"/v1/tasks/{i}",
            lambda i: f"/v1/jobs/{i}",
            lambda i: f"/v1/artifacts/{i}",
            lambda i: f"/v1/receipts/{i}",
        )
        for kind, path in zip(ids, paths, strict=True):
            home_response = client.get(path(ids[kind][0]), headers=headers)
            assert home_response.status_code == 200, (kind, home_response.text)
            foreign_response = client.get(path(ids[kind][1]), headers=headers)
            assert foreign_response.status_code == 403, (kind, foreign_response.text)
            assert foreign not in foreign_response.text
        foreign_pdf = client.get(f"/v1/artifacts/{ids['artifact'][1]}/pdf", headers=headers)
        assert foreign_pdf.status_code == 403, foreign_pdf.text
    finally:
        with engine.begin() as connection:
            for table, kind in (("receipts", "receipt"), ("artifacts", "artifact"),
                                ("jobs", "job"), ("tasks", "task")):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE id IN (:home, :foreign)"),
                    {"home": ids[kind][0], "foreign": ids[kind][1]},
                )
            connection.execute(
                text("DELETE FROM events WHERE project_id = :id"), {"id": foreign},
            )
            connection.execute(
                text("DELETE FROM projects WHERE id = :id"), {"id": foreign},
            )
