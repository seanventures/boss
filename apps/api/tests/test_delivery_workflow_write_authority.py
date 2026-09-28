"""Console workflow writes cannot act on a project selected by another principal."""

from uuid import uuid4

from sqlalchemy import text

from apps.api.app import main as api_main


def test_delivery_workflow_controls_reject_foreign_project(client, engine, monkeypatch):
    suffix = uuid4().hex[:10]
    home = "boss-dogfood"
    foreign = f"workflow_foreign_{suffix}"
    reader = f"workflow_reader_{suffix}"
    key = f"workflow-key-{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", key)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{key}:{reader}")
    headers = {"Authorization": f"Bearer {key}"}

    with engine.begin() as connection:
        workspace_id = connection.execute(
            text("SELECT workspace_id FROM projects WHERE id = :id"), {"id": home}
        ).scalar_one()
        connection.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) "
                 "VALUES (:id, :workspace_id, 'Foreign workflow', :id)"),
            {"id": foreign, "workspace_id": workspace_id},
        )
        api_main.members.upsert_member(
            connection, project_id=home, member_id=reader,
            display_name="Workflow reader", member_type="human", role_keys=[],
            permissions=[], actor="ceo",
        )

    try:
        writes = (
            ("POST", "/v1/delivery-workflows/custom-steps", {"label": "Private"}),
            ("PATCH", "/v1/delivery-workflows/custom-steps/private", {"label": "Changed"}),
            ("DELETE", "/v1/delivery-workflows/custom-steps/private", None),
            ("POST", "/v1/delivery-workflows/catalogue/install", {}),
            ("POST", "/v1/delivery-workflows/apply", {
                "workflow_key": "standard_product_delivery", "work_class": "project.new"
            }),
            ("POST", "/v1/delivery-workflows/create", {"name": "Private workflow"}),
            ("POST", "/v1/delivery-workflows/fork", {"workflow_key": "standard_product_delivery"}),
            ("POST", "/v1/delivery-workflows/save", {"definition_id": "private", "spec": {}}),
            ("DELETE", "/v1/delivery-workflows/private", None),
            ("POST", "/v1/delivery-workflows/runs/private/retry-step", {"step_id": "build"}),
            ("POST", "/v1/delivery-workflows/runs/private/recheck", {}),
            ("POST", "/v1/delivery-workflows/runs/private/deploy-target", {"host_id": "private"}),
            ("POST", "/v1/delivery-workflows/runs/private/input", {"values": {"title": "changed"}}),
            ("POST", "/v1/delivery-workflows/start", {"work_class": "project.new"}),
        )
        for method, path, extra in writes:
            if method == "DELETE":
                response = client.delete(path, params={"project_id": foreign}, headers=headers)
            else:
                response = client.request(
                    method, path, json={"project_id": foreign, **(extra or {})},
                    headers=headers,
                )
            assert response.status_code == 403, (method, path, response.text)
            assert foreign not in response.text
    finally:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM events WHERE project_id = :id"), {"id": foreign})
            connection.execute(text("DELETE FROM projects WHERE id = :id"), {"id": foreign})


def test_capability_tick_requires_worker_principal(client, engine, monkeypatch):
    from boss_engine.services import workflow_capability_runner

    suffix = uuid4().hex[:10]
    reader = f"tick_reader_{suffix}"
    key = f"tick-key-{suffix}"
    worker_key = f"worker-key-{suffix}"
    home = "boss-dogfood"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", f"{key},{worker_key}")
    monkeypatch.setattr(api_main.settings, "api_key_principals",
                        f"{key}:{reader},{worker_key}:test_workflow_worker")
    monkeypatch.setattr(api_main.settings, "worker_principal", "test_workflow_worker")
    with engine.begin() as connection:
        api_main.members.upsert_member(
            connection, project_id=home, member_id=reader,
            display_name="Tick reader", member_type="human", role_keys=[],
            permissions=[], actor="ceo",
        )

    calls = []
    monkeypatch.setattr(
        workflow_capability_runner, "tick",
        lambda *args, **kwargs: calls.append("tick") or {"performed": []},
    )
    monkeypatch.setattr(
        workflow_capability_runner, "sweep_gate_timeouts",
        lambda *args, **kwargs: calls.append("timeouts") or {"applied": []},
    )
    body = {"project_id": home, "worker_id": "boss-worker"}
    denied = client.post(
        "/v1/delivery-workflows/run-capabilities", json=body,
        headers={"Authorization": f"Bearer {key}"},
    )
    assert denied.status_code == 403, denied.text
    assert calls == []
    allowed = client.post(
        "/v1/delivery-workflows/run-capabilities", json=body,
        headers={"Authorization": f"Bearer {worker_key}"},
    )
    assert allowed.status_code == 200, allowed.text
    assert calls == ["tick", "timeouts"]


def test_delivery_input_rejects_foreign_target(client, engine, monkeypatch):
    suffix = uuid4().hex[:10]
    home = "boss-dogfood"
    foreign = f"target_foreign_{suffix}"
    target_id = f"target_foreign_{suffix}"
    key = f"ceo-key-{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", key)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{key}:ceo")

    with engine.begin() as connection:
        workspace_id = connection.execute(
            text("SELECT workspace_id FROM projects WHERE id = :id"), {"id": home}
        ).scalar_one()
        connection.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) "
                 "VALUES (:id, :workspace_id, 'Foreign target', :id)"),
            {"id": foreign, "workspace_id": workspace_id},
        )
        connection.execute(
            text("INSERT INTO deployment_targets "
                 "(id, project_id, app_name, repo, environment, target_kind, "
                 "target_ref, created_by) VALUES "
                 "(:id, :project_id, 'private-app', 'private/repo', 'production', "
                 "'test', 'test', 'test')"),
            {"id": target_id, "project_id": foreign},
        )
    try:
        response = client.post(
            "/v1/delivery-workflows/runs/missing/input",
            json={"project_id": home, "values": {"target_id": target_id}},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert response.status_code == 404, response.text
        assert foreign not in response.text
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM deployment_targets WHERE id = :id"), {"id": target_id}
            )
            connection.execute(text("DELETE FROM events WHERE project_id = :id"), {"id": foreign})
            connection.execute(text("DELETE FROM projects WHERE id = :id"), {"id": foreign})
