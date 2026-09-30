"""Multi-provider work tracker catalog, connect, and default selection."""

from __future__ import annotations

from unittest.mock import patch

from boss_engine.services.work_tracker_adapters import IssueCreateResult


PROJECT_ID = "boss-dogfood"


def test_work_tracker_catalog_includes_plane_default_and_alternatives(client):
    response = client.get("/v1/work-trackers/catalog")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["default_provider_key"] == "plane"
    keys = [item["provider_key"] for item in payload["items"]]
    assert keys == ["plane", "jira", "linear", "github_issues", "azure_devops"]
    plane = payload["items"][0]
    assert plane["is_default_option"] is True
    assert any(f["secret"] for f in plane["connection_fields"])


def test_list_work_trackers_seeds_plane_default(client):
    response = client.get(
        "/v1/work-trackers", params={"project_id": PROJECT_ID}
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["default_provider_key"] == "plane"
    keys = {item["provider_key"] for item in payload["items"]}
    assert "plane" in keys
    assert "jira" in keys
    assert "linear" in keys
    assert "github_issues" in keys
    assert "azure_devops" in keys
    plane = next(i for i in payload["items"] if i["provider_key"] == "plane")
    assert plane["is_default"] is True
    # Secrets never leak
    for item in payload["items"]:
        cfg = item.get("configuration") or {}
        for key in ("api_token", "token", "pat", "password"):
            if key in cfg:
                assert cfg[key] == "***"


def test_set_default_tracker_to_jira(client):
    listed = client.get("/v1/work-trackers", params={"project_id": PROJECT_ID})
    assert listed.status_code == 200
    # Skip live test; just flip default
    response = client.post(
        "/v1/work-trackers/set-default",
        json={
            "project_id": PROJECT_ID,
            "provider_key": "jira",
            "actor": "ceo",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["default_provider_key"] == "jira"
    jira = next(p for p in body["providers"] if p["provider_key"] == "jira")
    assert jira["is_default"] is True
    plane = next(p for p in body["providers"] if p["provider_key"] == "plane")
    assert plane["is_default"] is False

    # Restore Plane as default for other tests
    restore = client.post(
        "/v1/work-trackers/set-default",
        json={
            "project_id": PROJECT_ID,
            "provider_key": "plane",
            "actor": "ceo",
        },
    )
    assert restore.status_code == 200


def test_connect_github_issues_with_mocked_test(client):
    with patch(
        "boss_engine.services.work_tracker_adapters.GitHubIssuesWorkTrackerAdapter.test_connection",
        return_value={"ok": True, "message": "Connected to GitHub repo acme/app"},
    ):
        response = client.post(
            "/v1/work-trackers/connect",
            json={
                "project_id": PROJECT_ID,
                "provider_key": "github_issues",
                "actor": "ceo",
                "configuration": {
                    "owner": "acme",
                    "repo": "app",
                    "credential_ref": "env:BOSS_GITHUB_TOKEN",
                },
                "api_token": "ghs_test_token_not_real",
                "set_as_default": False,
                "test": True,
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider"]["provider_key"] == "github_issues"
    assert body["test"]["ok"] is True
    # Token redacted in response
    assert body["provider"]["configuration"].get("api_token") in (None, "***")


def test_execute_tracker_issue_create_uses_adapter(engine):
    from apps.api.app.config import get_settings
    from apps.api.app.services.work_orchestration import WorkOrchestrationService

    settings = get_settings()
    service = WorkOrchestrationService(settings)
    with engine.begin() as connection:
        service.seed_defaults(connection, settings.default_project_id)
        item = service.upsert_work_item(
            connection,
            project_id=settings.default_project_id,
            subject_type="bug",
            subject_id="tracker-multi-1",
            title="Broken login form",
            description="Users cannot sign in",
            strategic_value=90,
            customer_impact=95,
            urgency=90,
            cost_of_delay=80,
            risk_reduction=50,
            dependency_pressure=40,
            confidence=90,
            actor="product_manager",
        )
        binding = service.bind_tracker(
            connection,
            project_id=settings.default_project_id,
            work_item_id=item["id"],
            provider_key="github_issues",
            actor="product_manager",
        )
        with patch(
            "boss_engine.services.work_tracker_adapters.GitHubIssuesWorkTrackerAdapter.create_issue",
            return_value=IssueCreateResult(
                external_issue_id="99",
                external_key="42",
                external_url="https://github.com/acme/app/issues/42",
                external_state="open",
            ),
        ):
            # Ensure config has owner/repo so adapter would be callable
            connection.execute(
                __import__("sqlalchemy").text(
                    """
                    UPDATE work_tracker_providers
                    SET configuration = configuration || CAST(:cfg AS JSONB)
                    WHERE project_id = :project_id AND provider_key = 'github_issues'
                    """
                ),
                {
                    "project_id": settings.default_project_id,
                    "cfg": __import__("json").dumps(
                        {"owner": "acme", "repo": "app", "api_token": "x"}
                    ),
                },
            )
            result = service.execute_tracker_issue_create(
                connection,
                project_id=settings.default_project_id,
                binding_id=binding["id"],
            )
        assert result["ok"] is True
        assert result["external_key"] == "42"
        assert result["provider"] == "github_issues"
