"""QA Agent 3 — Community GA holes: list project_id defaults + factory WIP honesty."""

from __future__ import annotations

from apps.api.app.main import resolve_list_project_id


def test_resolve_list_project_id_defaults_to_settings():
    assert resolve_list_project_id(None) == "boss-dogfood"
    assert resolve_list_project_id("  ") == "boss-dogfood"
    assert resolve_list_project_id("proj_other") == "proj_other"


def test_operator_lists_default_project_id_instead_of_422(client):
    for path in (
        "/v1/tasks",
        "/v1/jobs",
        "/v1/events",
        "/v1/previews",
        "/v1/code-tasks",
        "/v1/factory/wip",
        "/v1/dashboard",
        "/v1/members",
    ):
        response = client.get(path)
        assert response.status_code < 500, f"{path} -> {response.status_code} {response.text[:200]}"
        assert response.status_code != 422, f"{path} should default project_id, got 422"
        assert response.status_code == 200, f"{path} -> {response.status_code} {response.text[:200]}"
        body = response.json()
        if isinstance(body, dict) and "project_id" in body:
            assert body["project_id"] == "boss-dogfood"


def test_factory_wip_advertises_fixture_and_github_plane(client):
    response = client.get("/v1/factory/wip")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["project_id"] == "boss-dogfood"
    assert body["fixture_supported"] is True
    assert body["github_plane"] in {
        "legacy_env",
        "mission_control_binding",
        "unknown",
    }
    assert "github_binding_id" in body
    assert "github_source" in body
    dumped = str(body).lower()
    assert "ghp_" not in dumped
    assert "github_pat" not in dumped
    assert "token=" not in dumped
