"""Setup status tells a Community operator the repository path that works on Community.

`/v1/setup/status` said "BOSS_GITHUB_TOKEN, BOSS_TARGET_GITHUB_OWNER and
BOSS_TARGET_GITHUB_REPO are not all set." Community's compose file never passes the TARGET pair
to its containers, so following that advice changed nothing. The connection screen is the path
that works there.
"""

import pytest

from apps.api.app import main as api_main
from boss_engine.services import repo_registry, setup_service


def _status(client, monkeypatch, edition):
    monkeypatch.setattr(api_main.settings, "boss_edition", edition)
    monkeypatch.setattr(api_main.settings, "target_github_owner", "")
    monkeypatch.setattr(api_main.settings, "target_github_repo", "")
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", "ceo-key")
    monkeypatch.setattr(api_main.settings, "api_key_principals", "ceo-key:ceo")
    response = client.get("/v1/setup/status", params={"project_id": "boss-dogfood"},
                          headers={"Authorization": "Bearer ceo-key"})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("edition", ["community", "oss", "local"])
def test_community_setup_status_points_at_the_connection_screen(client, monkeypatch, edition):
    body = _status(client, monkeypatch, edition)
    github = body["environment"]["github"]
    assert github["configured"] is False
    assert "Settings, Repo and model, Repository" in github["error"]
    assert github["connect_in"] == setup_service.REPOSITORY_CONNECT_PATH
    assert "are not all set" not in github["error"]
    assert "Community does not read BOSS_TARGET_GITHUB_OWNER" in github["error"]
    step = next(step for step in body["steps"] if step["id"] == "github")
    assert step["done"] is False
    assert "Settings, Repo and model, Repository" in step["how"]


def test_a_self_hosted_server_still_hears_about_its_environment(client, monkeypatch):
    body = _status(client, monkeypatch, "enterprise_self_hosted")
    error = body["environment"]["github"]["error"]
    assert "Settings, Repo and model, Repository" in error
    assert "BOSS_TARGET_GITHUB_OWNER and BOSS_TARGET_GITHUB_REPO in the API's environment" in error


def test_the_build_error_names_where_to_connect_a_repository(engine, monkeypatch):
    monkeypatch.setattr(api_main.settings, "target_github_owner", "")
    monkeypatch.setattr(api_main.settings, "target_github_repo", "")
    with engine.begin() as connection:
        with pytest.raises(repo_registry.RepositoryResolutionError) as raised:
            repo_registry.resolve_repository_target(
                connection, api_main.settings, "project-with-no-repository")
    assert "Settings, Repo and model, Repository" in str(raised.value)
