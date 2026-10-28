from __future__ import annotations

import json

from sqlalchemy import text

from apps.api.app import main as api_main


PROJECT_ID = "boss-dogfood"


class FakeGitHubAdapter:
    calls = 0
    list_calls = 0

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def repo_metadata(self) -> dict:
        type(self).calls += 1
        return {
            "id": 4242,
            "full_name": "seanheiney/boss",
            "default_branch": "main",
            "visibility": "private",
            "archived": False,
            "html_url": "https://github.com/seanheiney/boss",
            "owner": {"login": "seanheiney"},
            "permissions": {
                "pull": True,
                "triage": True,
                "push": True,
                "maintain": False,
                "admin": False,
            },
        }

    def list_accessible_repositories(self, *, per_page: int = 50, max_pages: int = 2):
        type(self).list_calls += 1
        return [
            {
                "full_name": "seanheiney/boss",
                "owner": "seanheiney",
                "name": "boss",
                "repository_id": 4242,
                "default_branch": "main",
                "visibility": "private",
                "archived": False,
                "permissions": {"pull": True, "push": True, "triage": True},
                "html_url": "https://github.com/seanheiney/boss",
            },
            {
                "full_name": "seanheiney/other",
                "owner": "seanheiney",
                "name": "other",
                "repository_id": 99,
                "default_branch": "main",
                "visibility": "private",
                "archived": False,
                "permissions": {"pull": True},
                "html_url": "https://github.com/seanheiney/other",
            },
        ]


def _create_secret_ref(
    client,
    *,
    ref_key: str = "BOSS_TEST_GITHUB_TOKEN",
    secret_kind: str = "api_token",
):
    return client.post(
        "/v1/secret-refs",
        json={
            "project_id": PROJECT_ID,
            "ref_kind": "env",
            "ref_key": ref_key,
            "provider": "github",
            "secret_kind": secret_kind,
            "actor": "ceo",
        },
    )


def _create_connection(client, **overrides):
    payload = {
        "project_id": PROJECT_ID,
        "connection_label": "boss-github",
        "auth_method": "personal_access_token",
        "credential_ref": "env:BOSS_TEST_GITHUB_TOKEN",
        "required_permissions": ["pull", "push"],
        "metadata": {"purpose": "software_factory"},
        "actor": "ceo",
    }
    payload.update(overrides)
    return client.post("/v1/github/connections", json=payload)


def _create_binding(client, connection_id: str, **overrides):
    payload = {
        "project_id": PROJECT_ID,
        "connection_id": connection_id,
        "repository": "seanheiney/boss",
        "role": "primary",
        "actor": "ceo",
    }
    payload.update(overrides)
    return client.post("/v1/github/repository-bindings", json=payload)


def test_project_github_connection_binding_and_receipted_test(
    client, engine, monkeypatch
):
    secret = _create_secret_ref(client)
    assert secret.status_code == 201
    monkeypatch.setenv("BOSS_TEST_GITHUB_TOKEN", "github-test-token-never-persist")
    FakeGitHubAdapter.calls = 0
    monkeypatch.setattr(
        api_main.github_connections,
        "adapter_factory",
        FakeGitHubAdapter,
    )

    created = _create_connection(client)
    assert created.status_code == 201
    github_connection = created.json()
    assert github_connection["status"] == "configured"
    assert github_connection["credential_configured"] is True
    assert "credential_ref" not in github_connection
    assert "github-test-token-never-persist" not in json.dumps(github_connection)

    bound = _create_binding(client, github_connection["id"])
    assert bound.status_code == 201
    binding = bound.json()
    assert binding["full_name"] == "seanheiney/boss"
    assert binding["role"] == "primary"
    assert binding["repository_id"] is None

    tested = client.post(
        f"/v1/github/connections/{github_connection['id']}/test",
        json={
            "project_id": PROJECT_ID,
            "repository_binding_id": binding["id"],
            "dedupe_key": "github-connection-test:boss:1",
            "actor": "ceo",
        },
    )
    assert tested.status_code == 200
    result = tested.json()
    assert result["status"] == "passed"
    assert result["duplicate"] is False
    assert result["connection"]["status"] == "active"
    assert result["connection"]["granted_permissions"] == ["pull", "push", "triage"]
    assert result["connection"]["missing_permissions"] == []
    assert result["repository_binding"]["repository_id"] == 4242
    assert result["repository_binding"]["default_branch"] == "main"
    assert "branches.write" in result["repository_binding"]["capabilities"]
    assert FakeGitHubAdapter.calls == 1

    replayed = client.post(
        f"/v1/github/connections/{github_connection['id']}/test",
        json={
            "project_id": PROJECT_ID,
            "repository_binding_id": binding["id"],
            "dedupe_key": "github-connection-test:boss:1",
            "actor": "ceo",
        },
    )
    assert replayed.status_code == 200
    assert replayed.json()["duplicate"] is True
    assert replayed.json()["receipt_id"] == result["receipt_id"]
    assert FakeGitHubAdapter.calls == 1

    second_binding = _create_binding(
        client,
        github_connection["id"],
        repository="seanheiney/boss-mirror",
        role="source",
    )
    assert second_binding.status_code == 201
    dedupe_collision = client.post(
        f"/v1/github/connections/{github_connection['id']}/test",
        json={
            "project_id": PROJECT_ID,
            "repository_binding_id": second_binding.json()["id"],
            "dedupe_key": "github-connection-test:boss:1",
            "actor": "ceo",
        },
    )
    assert dedupe_collision.status_code == 409
    assert "different GitHub connection test" in dedupe_collision.json()["detail"]
    assert FakeGitHubAdapter.calls == 1

    with engine.begin() as connection:
        receipt = connection.execute(
            text(
                """
                SELECT effect_kind, status, effect_scope, result
                FROM receipts
                WHERE id = :receipt_id
                """
            ),
            {"receipt_id": result["receipt_id"]},
        ).mappings().one()
        events = connection.execute(
            text(
                """
                SELECT event_type, data
                FROM events
                WHERE project_id = :project_id
                  AND entity_id IN (:connection_id, :binding_id)
                ORDER BY created_at
                """
            ),
            {
                "project_id": PROJECT_ID,
                "connection_id": github_connection["id"],
                "binding_id": binding["id"],
            },
        ).mappings().all()
    assert receipt["effect_kind"] == "github.repository.read"
    assert receipt["status"] == "succeeded"
    assert receipt["effect_scope"]["repository"] == "seanheiney/boss"
    assert receipt["result"]["status"] == "passed"
    assert {
        "github.connection_created",
        "github.repository_bound",
        "github.connection_test_passed",
    }.issubset({row["event_type"] for row in events})
    persisted = json.dumps(
        {"receipt": dict(receipt), "events": [dict(row) for row in events]}
    )
    assert "github-test-token-never-persist" not in persisted


def test_github_connection_permission_failure_and_lifecycle_are_fail_closed(
    client, monkeypatch
):
    assert _create_secret_ref(client).status_code == 201
    monkeypatch.setenv("BOSS_TEST_GITHUB_TOKEN", "github-lifecycle-secret")

    class PullOnlyAdapter(FakeGitHubAdapter):
        def repo_metadata(self) -> dict:
            payload = super().repo_metadata()
            payload["permissions"] = {"pull": True, "push": False}
            return payload

    monkeypatch.setattr(
        api_main.github_connections,
        "adapter_factory",
        PullOnlyAdapter,
    )
    github_connection = _create_connection(client).json()
    binding = _create_binding(client, github_connection["id"]).json()

    tested = client.post(
        f"/v1/github/connections/{github_connection['id']}/test",
        json={
            "project_id": PROJECT_ID,
            "repository_binding_id": binding["id"],
            "dedupe_key": "github-connection-test:missing-push",
            "actor": "ceo",
        },
    )
    assert tested.status_code == 200
    assert tested.json()["status"] == "failed"
    assert tested.json()["connection"]["status"] == "error"
    assert tested.json()["connection"]["missing_permissions"] == ["push"]

    disabled = client.post(
        f"/v1/github/connections/{github_connection['id']}/disable",
        json={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"
    blocked = client.post(
        f"/v1/github/connections/{github_connection['id']}/test",
        json={
            "project_id": PROJECT_ID,
            "repository_binding_id": binding["id"],
            "dedupe_key": "github-connection-test:disabled",
            "actor": "ceo",
        },
    )
    assert blocked.status_code == 409

    enabled = client.post(
        f"/v1/github/connections/{github_connection['id']}/enable",
        json={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    assert enabled.status_code == 200
    assert enabled.json()["status"] == "configured"

    revoked = client.post(
        f"/v1/github/connections/{github_connection['id']}/revoke",
        json={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    listed_bindings = client.get(
        "/v1/github/repository-bindings",
        params={"project_id": PROJECT_ID},
    )
    assert listed_bindings.json()["items"][0]["status"] == "disabled"
    cannot_enable = client.post(
        f"/v1/github/connections/{github_connection['id']}/enable",
        json={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    assert cannot_enable.status_code == 409
    assert "cannot be re-enabled" in cannot_enable.json()["detail"]


def test_github_connection_rejects_raw_secrets_bad_scope_and_unauthorized_actor(
    client,
):
    assert _create_secret_ref(client).status_code == 201

    raw_secret = _create_connection(
        client,
        metadata={"private_key": "not-allowed"},
    )
    assert raw_secret.status_code == 422
    assert "SecretRef" in raw_secret.json()["detail"]

    bad_ref = _create_connection(
        client,
        connection_label="missing-ref",
        credential_ref="env:BOSS_UNKNOWN_GITHUB_TOKEN",
    )
    assert bad_ref.status_code == 422

    unauthorized = _create_connection(
        client,
        connection_label="coo-cannot-connect",
        actor="coo",
    )
    assert unauthorized.status_code == 403


def test_github_app_connection_requires_non_secret_installation_identity(client):
    assert (
        _create_secret_ref(
            client,
            ref_key="BOSS_TEST_GITHUB_APP_PRIVATE_KEY",
            secret_kind="private_key",
        ).status_code
        == 201
    )
    incomplete = _create_connection(
        client,
        connection_label="github-app-incomplete",
        auth_method="github_app",
        credential_ref="env:BOSS_TEST_GITHUB_APP_PRIVATE_KEY",
        app_id="12345",
        installation_id=None,
    )
    assert incomplete.status_code == 422
    assert "installation_id" in incomplete.json()["detail"]

    complete = _create_connection(
        client,
        connection_label="github-app-complete",
        auth_method="github_app",
        credential_ref="env:BOSS_TEST_GITHUB_APP_PRIVATE_KEY",
        app_id="12345",
        installation_id="67890",
    )
    assert complete.status_code == 201
    payload = complete.json()
    assert payload["app_id"] == "12345"
    assert payload["installation_id"] == "67890"
    assert "credential_ref" not in payload


def test_github_easy_connect_links_repo_and_lists_accessible_repositories(
    client, monkeypatch
):
    monkeypatch.setenv("BOSS_EASY_GITHUB_TOKEN", "github-easy-connect-secret")
    FakeGitHubAdapter.calls = 0
    FakeGitHubAdapter.list_calls = 0
    monkeypatch.setattr(
        api_main.github_connections,
        "adapter_factory",
        FakeGitHubAdapter,
    )

    response = client.post(
        "/v1/github/easy-connect",
        json={
            "project_id": PROJECT_ID,
            "connection_label": "easy-project-github",
            "auth_method": "personal_access_token",
            "env_credential_key": "boss_easy_github_token",
            "repository": "seanheiney/boss",
            "role": "primary",
            "test_after_link": True,
            "actor": "ceo",
        },
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["credential_ref"] == "env:BOSS_EASY_GITHUB_TOKEN"
    assert payload["connection"]["status"] == "active"
    assert payload["connection"]["auth_method"] == "personal_access_token"
    assert "credential_ref" not in payload["connection"]
    assert payload["repository_binding"]["full_name"] == "seanheiney/boss"
    assert payload["repository_binding"]["role"] == "primary"
    assert payload["test"]["status"] == "passed"
    assert payload["test"]["receipt_id"]
    assert FakeGitHubAdapter.calls == 1
    assert "github-easy-connect-secret" not in json.dumps(payload)

    listed = client.get(
        f"/v1/github/connections/{payload['connection']['id']}/repositories",
        params={"project_id": PROJECT_ID, "limit": 20},
    )
    assert listed.status_code == 200, listed.text
    repos = listed.json()
    assert repos["connection_id"] == payload["connection"]["id"]
    assert {item["full_name"] for item in repos["items"]} == {
        "seanheiney/boss",
        "seanheiney/other",
    }
    assert FakeGitHubAdapter.list_calls == 1

    # Reusing the same env key should not fail SecretRef uniqueness when creating
    # another labeled connection for a second repository link path is blocked by
    # primary uniqueness — connect without repository succeeds with unique label.
    second = client.post(
        "/v1/github/easy-connect",
        json={
            "project_id": PROJECT_ID,
            "connection_label": "easy-project-github-secondary",
            "env_credential_key": "BOSS_EASY_GITHUB_TOKEN",
            "actor": "ceo",
        },
    )
    assert second.status_code == 201, second.text
    assert second.json()["repository_binding"] is None
    assert second.json()["test"] is None


def test_github_repository_binding_is_project_scoped_and_primary_is_unique(client):
    assert _create_secret_ref(client).status_code == 201
    github_connection = _create_connection(client).json()
    binding = _create_binding(client, github_connection["id"]).json()

    duplicate_primary = _create_binding(
        client,
        github_connection["id"],
        repository="seanheiney/boss-sandbox",
    )
    assert duplicate_primary.status_code == 409
    assert "primary repository" in duplicate_primary.json()["detail"]

    duplicate_connection = _create_connection(
        client,
        connection_label="BOSS-GITHUB",
    )
    assert duplicate_connection.status_code == 422
    assert "connection with this label" in duplicate_connection.json()["detail"]

    duplicate_repository = _create_binding(
        client,
        github_connection["id"],
        role="source",
    )
    assert duplicate_repository.status_code == 409
    assert "already bound" in duplicate_repository.json()["detail"]

    cross_project_read = client.get(
        f"/v1/github/repository-bindings/{binding['id']}",
        params={"project_id": "not-this-project"},
    )
    assert cross_project_read.status_code == 404
