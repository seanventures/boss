"""A project is not limited to one GitHub account: a second, separately labeled connection
(its own credential_ref) can hold its own repository binding, and the two connections' bindings
and tests never cross into each other."""
from __future__ import annotations

from boss_engine.config import get_settings
from boss_engine.services.github_connections import GitHubConnectionService
from boss_engine.services.secret_refs import SecretRefService

from ._multirepo_support import make_engine, make_world


class FakeAdapter:
    calls = 0

    def __init__(self, *_args, owner="example-org", repo="boss", **_kwargs) -> None:
        self._owner, self._repo = owner, repo

    def repo_metadata(self) -> dict:
        type(self).calls += 1
        return {
            "id": 4242,
            "full_name": f"{self._owner}/{self._repo}",
            "default_branch": "main",
            "visibility": "private",
            "archived": False,
            "html_url": f"https://github.com/{self._owner}/{self._repo}",
            "owner": {"login": self._owner},
            "permissions": {"pull": True, "push": True, "triage": True},
        }


def _connect(service, conn, pid, *, label, env_key, monkeypatch):
    secret_refs = SecretRefService()
    monkeypatch.setenv(env_key, f"token-for-{label}")
    secret_refs.create_secret_ref(
        conn,
        project_id=pid,
        ref_kind="env",
        ref_key=env_key,
        provider="github",
        status="active",
        metadata={"purpose": "test"},
        actor="test",
    )
    return service.create_connection(
        conn,
        project_id=pid,
        connection_label=label,
        auth_method="personal_access_token",
        credential_ref=f"env:{env_key}",
        app_id=None,
        installation_id=None,
        api_base_url="https://api.github.com",
        required_permissions=["pull"],
        metadata={},
        actor="test",
    )


def test_a_project_can_bind_repos_across_two_different_github_accounts(monkeypatch):
    engine = make_engine()
    world = make_world(engine)
    pid = world["projects"][0]
    service = GitHubConnectionService(get_settings(), adapter_factory=FakeAdapter)
    FakeAdapter.calls = 0

    with engine.begin() as conn:
        first = _connect(
            service, conn, pid, label="acme-account", env_key="BOSS_TEST_ACME_TOKEN",
            monkeypatch=monkeypatch,
        )
        second = _connect(
            service, conn, pid, label="globex-account", env_key="BOSS_TEST_GLOBEX_TOKEN",
            monkeypatch=monkeypatch,
        )
    assert first["id"] != second["id"]

    with engine.begin() as conn:
        first_binding = service.create_repository_binding(
            conn, project_id=pid, connection_id=first["id"], repository="example-org/boss",
            role="primary", default_branch=None, metadata={}, actor="test",
        )
        second_binding = service.create_repository_binding(
            conn, project_id=pid, connection_id=second["id"], repository="other-org/other",
            role="source", default_branch=None, metadata={}, actor="test",
        )
    assert first_binding["full_name"] == "example-org/boss"
    assert second_binding["full_name"] == "other-org/other"

    with engine.begin() as conn:
        token_first = service.token_for_repository(
            conn, project_id=pid, owner="example-org", repo="boss"
        )
        token_second = service.token_for_repository(
            conn, project_id=pid, owner="other-org", repo="other"
        )
    assert token_first == "token-for-acme-account"
    assert token_second == "token-for-globex-account"
    assert token_first != token_second

    with engine.begin() as conn:
        tested_first = service.test_connection(
            conn, project_id=pid, connection_id=first["id"], binding_id=first_binding["id"],
            dedupe_key="github-connection-test:acme:1", actor="test",
        )
    assert tested_first["status"] == "passed"
    assert FakeAdapter.calls == 1

    with engine.begin() as conn:
        service.transition_connection(
            conn, project_id=pid, connection_id=first["id"], action="disable", actor="test",
        )

    with engine.begin() as conn:
        tested_second = service.test_connection(
            conn, project_id=pid, connection_id=second["id"], binding_id=second_binding["id"],
            dedupe_key="github-connection-test:globex:1", actor="test",
        )
    assert tested_second["status"] == "passed"
    assert tested_second["connection"]["id"] == second["id"]
    assert FakeAdapter.calls == 2

    with engine.begin() as conn:
        first_bindings = service.list_repository_bindings(
            conn, project_id=pid, connection_id=first["id"]
        )
        second_bindings = service.list_repository_bindings(
            conn, project_id=pid, connection_id=second["id"]
        )
    assert [b["full_name"] for b in first_bindings] == ["example-org/boss"]
    assert [b["full_name"] for b in second_bindings] == ["other-org/other"]
