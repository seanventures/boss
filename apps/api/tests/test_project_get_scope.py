"""Authenticated project reads must not expose a sibling project's lists."""

from uuid import uuid4

from sqlalchemy import text
from starlette.requests import Request

from apps.api.app import main as api_main


PROJECT = "boss-dogfood"


def test_project_scope_is_found_behind_a_root_path():
    scope = {
        "type": "http", "method": "GET", "scheme": "http",
        "server": ("test", 80), "client": ("test", 1),
        "root_path": "/proxy", "path": "/proxy/v1/tasks",
        "query_string": b"", "headers": [],
    }
    assert api_main.requested_get_project_id(Request(scope)) == PROJECT


def test_project_get_scope_checks_explicit_and_default_lists(
    client, engine, monkeypatch,
):
    suffix = uuid4().hex[:10]
    reader = f"get_scope_reader_{suffix}"
    key = f"get-scope-key-{suffix}"
    foreign_project = f"get_scope_foreign_{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", key)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{key}:{reader}")
    headers = {"Authorization": f"Bearer {key}"}

    with engine.begin() as connection:
        workspace_id = connection.execute(
            text("SELECT workspace_id FROM projects WHERE id=:project_id"),
            {"project_id": PROJECT},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) "
                 "VALUES (:id, :workspace_id, 'Private read project', :id)"),
            {"id": foreign_project, "workspace_id": workspace_id},
        )
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id=reader,
            display_name="Project read test", member_type="human", role_keys=[],
            permissions=[], actor="ceo",
        )

    try:
        for path in ("/v1/tasks", "/v1/events", "/v1/members"):
            home = client.get(path, params={"project_id": PROJECT}, headers=headers)
            assert home.status_code == 200, (path, home.text)

            default = client.get(path, headers=headers)
            assert default.status_code == 200, (path, default.text)
            assert default.json()["project_id"] == PROJECT

            denied = client.get(
                path, params={"project_id": foreign_project}, headers=headers,
            )
            assert denied.status_code == 403, (path, denied.text)
            assert "Private read project" not in denied.text

        path_denied = client.get(
            f"/v1/projects/{foreign_project}/agents", headers=headers,
        )
        assert path_denied.status_code == 403, path_denied.text
        for path in ("/v1/settings", "/v1/status", "/v1/dashboard/poll"):
            denied_query = client.get(
                path, params={"project_id": foreign_project}, headers=headers,
            )
            assert denied_query.status_code == 403, (path, denied_query.text)

        # FastAPI consumes the final repeated query value; the boundary must
        # authorize that same value in either order.
        duplicate_foreign = client.get(
            "/v1/tasks",
            params=[("project_id", PROJECT), ("project_id", foreign_project)],
            headers=headers,
        )
        assert duplicate_foreign.status_code == 403, duplicate_foreign.text
        duplicate_home = client.get(
            "/v1/tasks",
            params=[("project_id", foreign_project), ("project_id", PROJECT)],
            headers=headers,
        )
        assert duplicate_home.status_code == 200, duplicate_home.text
        assert duplicate_home.json()["project_id"] == PROJECT

        monkeypatch.setattr(api_main.settings, "default_project_id", foreign_project)
        for path in (
            "/v1/tasks", "/v1/events", "/v1/members", "/v1/activity",
            "/v1/capability-manifest", "/v1/capability-manifest/table",
        ):
            denied_default = client.get(path, headers=headers)
            assert denied_default.status_code == 403, (path, denied_default.text)
        monkeypatch.setattr(api_main.settings, "default_project_id", PROJECT)

        # Capability and session reads remain available to a valid transport principal.
        assert client.get("/health").status_code == 200
        assert client.get("/v1/auth/session", headers=headers).status_code == 200
        assert client.get(
            "/v1/auth/session", params={"project_id": foreign_project},
            headers=headers,
        ).status_code == 200
        assert client.get(
            "/v1/edition", params={"project_id": PROJECT}, headers=headers,
        ).status_code == 200
        assert client.get(
            "/v1/edition", params={"project_id": foreign_project}, headers=headers,
        ).status_code == 403
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM events WHERE project_id=:project_id"),
                {"project_id": foreign_project},
            )
            connection.execute(
                text("DELETE FROM projects WHERE id=:project_id"),
                {"project_id": foreign_project},
            )
