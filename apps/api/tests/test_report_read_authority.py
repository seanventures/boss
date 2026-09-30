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
    writer = f"report_writer_{suffix}"
    writer_key = f"writer-report-key-{suffix}"
    foreign_project = f"report_foreign_{suffix}"
    home_code_run = f"crun_home_{suffix}"
    foreign_code_run = f"crun_foreign_{suffix}"
    home_workflow_run = f"wfr_home_{suffix}"
    foreign_workflow_run = f"wfr_foreign_{suffix}"

    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", f"{key},{foreign_key},{writer_key}")
    monkeypatch.setattr(
        api_main.settings, "api_key_principals",
        f"{key}:{reader},{foreign_key}:{foreign_reader},{writer_key}:{writer}",
    )
    headers = {"Authorization": f"Bearer {key}"}
    foreign_headers = {"Authorization": f"Bearer {foreign_key}"}
    writer_headers = {"Authorization": f"Bearer {writer_key}"}

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
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id=writer,
            display_name="Report publisher", member_type="human", role_keys=[],
            permissions=["audit_export"], actor="ceo",
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

    home_detail = client.get(
        f"/v1/workflow-runs/{home_workflow_run}",
        params={"project_id": PROJECT}, headers=headers,
    )
    assert home_detail.status_code == 200, home_detail.text
    assert home_detail.json()["id"] == home_workflow_run

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

    for params in ({"project_id": foreign_project}, {"project_id": PROJECT}):
        denied_detail = client.get(
            f"/v1/workflow-runs/{foreign_workflow_run}",
            params=params, headers=headers,
        )
        assert denied_detail.status_code == 403, denied_detail.text
        assert "foreign-private-report" not in denied_detail.text
    foreign_detail = client.get(
        f"/v1/workflow-runs/{foreign_workflow_run}",
        params={"project_id": foreign_project}, headers=foreign_headers,
    )
    assert foreign_detail.status_code == 200, foreign_detail.text
    assert foreign_detail.json()["id"] == foreign_workflow_run

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

    report_path = f"/v1/workflow-runs/{home_workflow_run}/report"
    foreign_report_path = f"/v1/workflow-runs/{foreign_workflow_run}/report"
    for path, params, auth, expected_status in (
        (report_path, {"project_id": PROJECT}, headers, 403),
        (foreign_report_path, {"project_id": foreign_project}, writer_headers, 403),
        (foreign_report_path, {"project_id": PROJECT}, writer_headers, 404),
        (report_path, {"project_id": PROJECT}, None, 401),
    ):
        denied = client.post(path, params=params, headers=auth)
        assert denied.status_code == expected_status, (path, denied.text)
        assert "foreign-private-report" not in denied.text

    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM artifacts WHERE artifact_type = 'workflow_report' "
                 "AND project_id IN (:home, :foreign)"),
            {"home": PROJECT, "foreign": foreign_project},
        ).scalar_one() == 0

    published = client.post(
        report_path, params={"project_id": PROJECT}, headers=writer_headers,
    )
    assert published.status_code == 200, published.text
    assert published.json()["created"] is True
    artifact_id = published.json()["artifact_id"]
    repeated = client.post(report_path, headers=writer_headers)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == {"ok": True, "artifact_id": artifact_id, "created": False}
    with engine.begin() as connection:
        artifact = connection.execute(
            text("SELECT project_id, content->>'run_id' AS run_id "
                 "FROM artifacts WHERE id = :id"),
            {"id": artifact_id},
        ).mappings().one()
        assert artifact["project_id"] == PROJECT
        assert artifact["run_id"] == home_workflow_run
        assert connection.execute(
            text("SELECT count(*) FROM artifacts WHERE artifact_type = 'workflow_report' "
                 "AND project_id = :foreign"),
            {"foreign": foreign_project},
        ).scalar_one() == 0
