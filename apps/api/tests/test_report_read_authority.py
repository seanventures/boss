"""Retrospectives and workflow reports require membership in the stored run project."""

from uuid import uuid4

from sqlalchemy import text

from apps.api.app import main as api_main


PROJECT = "boss-dogfood"


def test_run_reports_require_stored_project_membership(client, engine, monkeypatch):
    suffix = uuid4().hex[:10]
    reader = f"report_reader_{suffix}"
    key = f"report-key-{suffix}"
    foreign_reader = f"foreign_reader_{suffix}"
    foreign_key = f"foreign-report-key-{suffix}"
    foreign_project = f"report_foreign_{suffix}"
    home_code_run = f"crun_home_{suffix}"
    foreign_code_run = f"crun_foreign_{suffix}"
    home_workflow_run = f"wfr_home_{suffix}"
    foreign_workflow_run = f"wfr_foreign_{suffix}"

    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", f"{key},{foreign_key}")
    monkeypatch.setattr(
        api_main.settings, "api_key_principals",
        f"{key}:{reader},{foreign_key}:{foreign_reader}",
    )
    headers = {"Authorization": f"Bearer {key}"}
    foreign_headers = {"Authorization": f"Bearer {foreign_key}"}

    with engine.begin() as connection:
        workspace_id = connection.execute(
            text("SELECT workspace_id FROM projects WHERE id = :id"),
            {"id": PROJECT},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) "
                 "VALUES (:id, :workspace_id, 'Foreign report test', :id)"),
            {"id": foreign_project, "workspace_id": workspace_id},
        )
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id=reader,
            display_name="Report reader", member_type="human", role_keys=[],
            permissions=[], actor="ceo",
        )
        api_main.members.upsert_member(
            connection, project_id=foreign_project, member_id=foreign_reader,
            display_name="Foreign report reader", member_type="human", role_keys=[],
            permissions=[], actor="ceo",
        )
        for code_run, workflow_run, project_id, label in (
            (home_code_run, home_workflow_run, PROJECT, "home-visible-report"),
            (foreign_code_run, foreign_workflow_run, foreign_project, "foreign-private-report"),
        ):
            definition_id = f"wfd_{workflow_run}"
            version_id = f"wfv_{workflow_run}"
            connection.execute(
                text("INSERT INTO code_task_runs (id, project_id, title, status, phase) "
                     "VALUES (:id, :project_id, :title, 'completed', 'done')"),
                {"id": code_run, "project_id": project_id, "title": label},
            )
            connection.execute(
                text("INSERT INTO workflow_definitions "
                     "(id, project_id, workflow_key, name, created_by) "
                     "VALUES (:id, :project_id, :workflow_key, :name, 'ceo')"),
                {"id": definition_id, "project_id": project_id,
                 "workflow_key": definition_id, "name": label},
            )
            connection.execute(
                text("INSERT INTO workflow_versions "
                     "(id, project_id, workflow_definition_id, version, "
                     "spec, content_hash, created_by) "
                     "VALUES (:id, :project_id, :definition_id, 1, '{}'::jsonb, :hash, 'ceo')"),
                {"id": version_id, "project_id": project_id,
                 "definition_id": definition_id, "hash": version_id},
            )
            connection.execute(
                text("INSERT INTO workflow_runs "
                     "(id, project_id, workflow_definition_id, workflow_version_id, "
                     "actor, status, input) "
                     "VALUES (:id, :project_id, :definition_id, :version_id, "
                     "'ceo', 'completed', CAST(:input AS jsonb))"),
                {"id": workflow_run, "project_id": project_id,
                 "definition_id": definition_id, "version_id": version_id,
                 "input": '{"directive":"' + label + '"}'},
            )

    for path in (
        f"/v1/code-tasks/{home_code_run}/retrospective",
        f"/v1/workflow-runs/{home_workflow_run}/report",
    ):
        response = client.get(path, headers=headers)
        assert response.status_code == 200, (path, response.text)
        assert response.json()["ok"] is True
        assert "home-visible-report" in response.text

    for path in (
        f"/v1/code-tasks/{foreign_code_run}/retrospective",
        f"/v1/workflow-runs/{foreign_workflow_run}/report",
    ):
        for params in ({}, {"project_id": foreign_project}, {"project_id": PROJECT}):
            response = client.get(path, params=params, headers=headers)
            assert response.status_code == 403, (path, params, response.text)
            assert "foreign-private-report" not in response.text

        allowed = client.get(path, headers=foreign_headers)
        assert allowed.status_code == 200, (path, allowed.text)
        assert allowed.json()["ok"] is True
        assert "foreign-private-report" in allowed.text

    for path in (
        f"/v1/code-tasks/{foreign_code_run}/retrospective",
        f"/v1/workflow-runs/{foreign_workflow_run}/report",
    ):
        response = client.get(path)
        assert response.status_code == 401, response.text
        assert "foreign-private-report" not in response.text

    assert client.get(
        "/v1/code-tasks/does-not-exist/retrospective", headers=headers,
    ).status_code == 404
    assert client.get(
        "/v1/workflow-runs/does-not-exist/report", headers=headers,
    ).status_code == 404
