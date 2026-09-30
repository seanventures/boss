"""Code-task and preview details stay inside the caller's project."""

from uuid import uuid4

from sqlalchemy import text

from apps.api.app import main as api_main


PROJECT = "boss-dogfood"


def test_run_and_preview_detail_reads_require_stored_project_membership(
    client, engine, monkeypatch, tmp_path,
):
    suffix = uuid4().hex[:10]
    reader = f"detail_reader_{suffix}"
    key = f"detail-key-{suffix}"
    foreign_project = f"detail_foreign_{suffix}"
    home_run = f"crun_home_{suffix}"
    foreign_run = f"crun_foreign_{suffix}"
    home_preview = f"prev_home_{suffix}"
    foreign_preview = f"prev_foreign_{suffix}"
    secret = f"foreign-private-log-{suffix}"
    log_file = tmp_path / "foreign-preview.log"
    log_file.write_text(secret)

    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", key)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{key}:{reader}")
    headers = {"Authorization": f"Bearer {key}"}

    with engine.begin() as connection:
        workspace_id = connection.execute(
            text("SELECT workspace_id FROM projects WHERE id = :id"),
            {"id": PROJECT},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) "
                 "VALUES (:id, :workspace_id, 'Foreign detail test', :id)"),
            {"id": foreign_project, "workspace_id": workspace_id},
        )
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id=reader,
            display_name="Detail reader", member_type="human", role_keys=[],
            permissions=[], actor="ceo",
        )
        for run_id, project_id, title in (
            (home_run, PROJECT, "home-visible-run"),
            (foreign_run, foreign_project, "foreign-private-run"),
        ):
            connection.execute(
                text("INSERT INTO code_task_runs "
                     "(id, project_id, title, status, phase) "
                     "VALUES (:id, :project_id, :title, 'completed', 'done')"),
                {"id": run_id, "project_id": project_id, "title": title},
            )
        for preview_id, project_id, run_id, log_path in (
            (home_preview, PROJECT, home_run, None),
            (foreign_preview, foreign_project, foreign_run, str(log_file)),
        ):
            connection.execute(
                text("INSERT INTO app_previews "
                     "(id, project_id, run_id, status, mode, log_path) "
                     "VALUES (:id, :project_id, :run_id, 'failed', 'process', :log_path)"),
                {"id": preview_id, "project_id": project_id,
                 "run_id": run_id, "log_path": log_path},
            )

    run_suffixes = ("live", "activity", "feedback", "deployments", "")
    for suffix_path in run_suffixes:
        path = f"/v1/code-tasks/{home_run}"
        if suffix_path:
            path += f"/{suffix_path}"
        response = client.get(path, headers=headers)
        assert response.status_code == 200, (path, response.text)

    for suffix_path in run_suffixes:
        path = f"/v1/code-tasks/{foreign_run}"
        if suffix_path:
            path += f"/{suffix_path}"
        response = client.get(path, headers=headers)
        assert response.status_code == 403, (path, response.text)
        assert secret not in response.text
        assert "foreign-private-run" not in response.text

    assert client.get(f"/v1/previews/{home_preview}/gate", headers=headers).status_code == 200
    assert client.get(
        f"/v1/previews/{home_preview}/logs", params={"stream": False}, headers=headers,
    ).status_code == 200
    for path, params in (
        (f"/v1/previews/{foreign_preview}/gate", {}),
        (f"/v1/previews/{foreign_preview}/logs", {"stream": False}),
        (f"/v1/previews/{foreign_preview}/logs", {"stream": True}),
    ):
        response = client.get(path, params=params, headers=headers)
        assert response.status_code == 403, (path, response.text)
        assert secret not in response.text

    for stream in (False, True):
        response = client.get(
            f"/v1/previews/{foreign_preview}/logs", params={"stream": stream},
        )
        assert response.status_code == 401, response.text
        assert secret not in response.text

    # Only the browser preview and feedback remain capability-gated exemptions.
    assert client.get(f"/v1/previews/{foreign_preview}/web/").status_code != 401
