"""A boss.deploy.yaml can read only the app secrets an operator set on its own target.

The manifest comes from the repository — often from a pull request an agent wrote — and every
value it reads is written to the host and exported to the repository's own install commands.
So `{secret: NAME}` must never reach the GitHub connection's token, a fleet host's SSH key or
another target's secrets, however the name is spelled. And whoever approves the deployment sees
which BOSS secret each key carries, not only the key's name.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.tests.test_a_stack_deploys_only_through_an_approved_changeset import (  # noqa: F401
    HOME,
    _approve_and_execute,
    stack,
)
from boss_engine.services.secret_backends import durable_secret_backend
from boss_engine.services.secrets_service import put_secret, ref_key_for

GITHUB_TOKEN = "ghp_this-is-the-connection-token-0001"
HOST_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nhost-key-material\n-----END OPENSSH"
APP_VALUE = "app-secret-value-for-this-target-42"


def _manifest(secret_name: str) -> str:
    return ("version: 1\nname: shop\n"
            f"env:\n  X: {{secret: {secret_name}}}\n"
            "install: ['./install.sh']\n")


def _put(engine, *, name, value, scope_type, scope_id, kind="api_token", provider=None):
    with engine.begin() as connection:
        stored = put_secret(connection, api_main.settings, project_id=HOME, name=name,
                            value=value, secret_kind=kind,
                            backend=durable_secret_backend(api_main.settings),
                            scope_type=scope_type, scope_id=scope_id, actor="test")
        if provider:
            connection.execute(text("UPDATE secret_refs SET provider = :p WHERE id = :i"),
                               {"p": provider, "i": stored["secret_ref_id"]})
    return stored


def _plan(client, stack):
    return client.post("/v1/deployments/stack/plan", json={
        "project_id": HOME, "target_id": stack["target"]["id"], "ref": "main", "actor": "ceo"})


@pytest.fixture()
def planted(engine, stack):
    """The secrets an attacker's manifest would want, stored the way BOSS stores them."""
    github = _put(engine, name="github_github", value=GITHUB_TOKEN, scope_type="project",
                  scope_id=HOME, provider="github")
    host_id = stack["target"]["host_id"]
    host = _put(engine, name="host_key", value=HOST_KEY, scope_type="deployment_host",
                scope_id=host_id, kind="ssh_private_key")
    return {"github": github, "host": host,
            "names": ["github_github", github["ref_key"], host["ref_key"],
                      ref_key_for(host_id, "host_key")]}


def _leaked(stack) -> bool:
    env = "".join(stack["host"].env_files())
    return GITHUB_TOKEN in env or "host-key-material" in env


def test_a_manifest_cannot_read_the_github_token_or_a_host_key_by_any_name(
        client, stack, planted):
    for name in planted["names"]:
        stack["github"].read_file = lambda *a, _n=name: _manifest(_n)
        response = _plan(client, stack)
        assert response.status_code == 422, (name, response.text)
        detail = response.json()["detail"]
        assert "not have" in detail["headline"] and stack["target"]["id"] in detail["action"]
    assert stack["host"].writes() == [] and not _leaked(stack)


def test_a_private_key_scoped_to_the_target_is_still_refused(client, engine, stack):
    _put(engine, name="deploy_key", value=HOST_KEY, scope_type="deployment_target",
         scope_id=stack["target"]["id"], kind="ssh_private_key")
    stack["github"].read_file = lambda *a: _manifest("deploy_key")
    assert _plan(client, stack).status_code == 422


def test_another_targets_secret_is_not_this_targets(client, engine, stack):
    other = client.post("/v1/deployments/stack/targets", json={
        "project_id": HOME, "host_id": stack["target"]["host_id"], "repository": "acme/other",
        "app_name": "other-app", "actor": "ceo"}).json()
    _put(engine, name="app_key", value=APP_VALUE, scope_type="deployment_target",
         scope_id=other["id"])
    stack["github"].read_file = lambda *a: _manifest("app_key")
    assert _plan(client, stack).status_code == 422


def test_a_secret_set_on_the_target_is_delivered_and_named_in_the_decision(
        client, engine, stack, planted):
    _put(engine, name="app_key", value=APP_VALUE, scope_type="deployment_target",
         scope_id=stack["target"]["id"])
    stack["github"].read_file = lambda *a: _manifest("app_key")
    response = _plan(client, stack)
    assert response.status_code == 200, response.text
    deployment = response.json()["deployment"]
    assert deployment["manifest"]["secret_env"] == {"X": "app_key"}
    with engine.begin() as connection:
        recommendation = connection.execute(
            text("SELECT recommendation FROM decisions WHERE id = :d"),
            {"d": deployment["decision_id"]}).scalar_one()
    assert recommendation["env_secrets"] == {"X": "app_key"}

    result, _ = _approve_and_execute(client, engine, deployment)
    assert result["status"] == "healthy", result
    assert f"X='{APP_VALUE}'" in "".join(stack["host"].env_files())
    assert APP_VALUE not in str(result)
    assert not _leaked(stack)
