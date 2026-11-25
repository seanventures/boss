"""A GitHub credential is only ever sent to its OWN host (BOSSAPP-1286 review).

The token and the API base used to be chosen separately: a GHES connection's token was probed
against api.github.com (repo_access.resolve without the base), and the install's own token
(BOSS_GITHUB_TOKEN / GITHUB_TOKEN) or the Setup App's token was sent to a GHES-bound repository's
host (github_token_for with the env token first, the stack deploy reader, CI). The one resolver now
returns the credential and its base TOGETHER, each candidate carries the base it belongs to, and
a candidate of another host is never minted or sent.

The world: one project; a GHES connection with ``platform/gateway`` bound to it; the install's
env token and Setup App on github.com; an unbound github.com repository. Every (token, host) pair
any path produces is recorded and must be one of the allowed pairs.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from boss_engine.identity import github_urls

from ._ops2_support import fresh_engine, make_project

GHES = "https://ghe.northgate.example/api/v3"
GHES_TOKEN, ENV_TOKEN, APP_TOKEN = "ghes-connection-token", "install-env-token", "setup-app-token"
ALLOWED = {(GHES_TOKEN, GHES), (ENV_TOKEN, github_urls.PUBLIC_API),
           (APP_TOKEN, github_urls.PUBLIC_API)}


@pytest.fixture(scope="module")
def eng():
    engine, drop = fresh_engine("ghhost")
    yield engine
    drop()


@pytest.fixture()
def world(eng, monkeypatch):
    for name in (github_urls.API_BASE_ENV, github_urls.WEB_BASE_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", ENV_TOKEN)
    monkeypatch.setenv("BOSS_GITHUB_TOKEN", ENV_TOKEN)
    project = make_project(eng)
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO github_connections (id, project_id, connection_label, auth_method, "
            "credential_ref, status, api_base_url, created_by) VALUES (:c, :p, 'GHES', "
            "'personal_access_token', 'env:GHES_TOKEN', 'active', :b, 'ceo')"),
            {"p": project, "b": GHES, "c": f"ghc_{project}"})
        c.execute(text(
            "INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, "
            "full_name, role, created_by) VALUES (:b, :p, :c, 'platform', "
            "'gateway', 'platform/gateway', 'primary', 'ceo')"),
            {"p": project, "b": f"ghb_{project}", "c": f"ghc_{project}"})

    from boss_engine.identity import github_setup
    from boss_engine.services.github_connections import GitHubConnectionService

    def token_for_repository(self, connection, *, project_id, owner, repo):
        return GHES_TOKEN if f"{owner}/{repo}" == "platform/gateway" else None

    monkeypatch.setattr(GitHubConnectionService, "token_for_repository", token_for_repository)
    monkeypatch.setattr(GitHubConnectionService, "token_for_connection",
                        lambda self, c, *, project_id, connection_id: GHES_TOKEN)
    monkeypatch.setattr(github_setup, "app_config",
                        lambda db, org_id=None: {"source": "setup", "app_id": "1", "slug": "boss"})
    monkeypatch.setattr(github_setup, "control_plane_token", lambda db, o, r: APP_TOKEN)
    from boss_engine.config import get_settings

    settings = get_settings().model_copy(update={"github_token": ENV_TOKEN})
    return project, settings


class _Recorder:
    """An httpx.Client stand-in for repo_access's probe: records (token, base) and says the
    credential reaches the repository."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def get(self, url, headers=None):
        import httpx

        token = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
        base = url.split("/repos/", 1)[0]
        self.sent.append((token, base))
        return httpx.Response(200, json={"permissions": {"push": True}},
                              request=httpx.Request("GET", url))


def test_every_path_pairs_a_token_with_its_own_host(eng, world):
    from boss_engine.services import repo_access
    from boss_engine.services.github_connections import api_base_for_repository, github_access_for
    from boss_engine.services.stack_deploy import github_source

    project, settings = world
    repo_access.reset_probe_cache()
    pairs: list[tuple[str, str]] = []
    rec = _Recorder()
    with eng.begin() as c:
        # The GHES-bound repository: only the GHES token, only to GHES.
        got = repo_access.resolve(c, settings, project, "platform", "gateway", need="push",
                                  client=rec)
        assert got.ok and got.token == GHES_TOKEN and got.api_base == GHES
        pairs.append((got.token, got.api_base))
        pairs.append(github_access_for(c, settings, project, "platform", "gateway"))
        src = github_source(c, settings, project, "platform", "gateway")
        pairs.append((src._token, src.API))  # noqa: SLF001
        assert api_base_for_repository(c, project, "platform", "gateway") == GHES

        # An unbound github.com repository: the install's tokens, only to api.github.com. The GHES
        # connection's token is NOT tried against it (its host differs).
        got = repo_access.resolve(c, settings, project, "someone", "public-lib", need="read",
                                  client=rec)
        assert got.ok and got.api_base == github_urls.PUBLIC_API
        pairs.append((got.token, got.api_base))
        # With no install credential, the GHES token is still not tried against github.com.
        bare = settings.model_copy(update={"github_token": None})
        import boss_engine.identity.github_setup as _gs

        real_mint, _gs.control_plane_token = _gs.control_plane_token, lambda db, o, r: None
        try:
            none = repo_access.resolve(c, bare, project, "someone", "public-lib", need="read",
                                       client=rec)
        finally:
            _gs.control_plane_token = real_mint
        assert not none.ok and none.token is None, none.tried
        assert {t["outcome"] for t in none.tried if t["source"] == "connection"} <= {"other_host"}
        pairs.append(github_access_for(c, settings, project, "someone", "public-lib"))
        src = github_source(c, settings, project, "someone", "public-lib")
        pairs.append((src._token, src.API))  # noqa: SLF001
        # The base never falls back to "the project's primary connection" for an unbound repo.
        assert api_base_for_repository(c, project, "someone", "public-lib") == github_urls.PUBLIC_API
    pairs.extend(rec.sent)
    crossed = [p for p in pairs if p[0] and p not in ALLOWED]
    assert not crossed, f"a token reached another host: {crossed}"
    assert (GHES_TOKEN, GHES) in pairs and (ENV_TOKEN, github_urls.PUBLIC_API) in pairs or \
        (APP_TOKEN, github_urls.PUBLIC_API) in pairs


def test_a_persons_github_com_token_is_never_sent_to_a_ghes_host():
    from types import SimpleNamespace

    from boss_engine.services.change_review import person_adapter

    base = SimpleNamespace(api_base_url=GHES, settings=None, owner="platform", repo="gateway")
    assert person_adapter(base, "persons-github-com-token") is base


GHES_WEB = "https://ghe.northgate.example"
ALLOWED_WEB = {(GHES_TOKEN, GHES_WEB), (ENV_TOKEN, github_urls.PUBLIC_WEB),
               (APP_TOKEN, github_urls.PUBLIC_WEB), ("", GHES_WEB), ("", github_urls.PUBLIC_WEB)}


def test_clone_deploy_preview_agent_and_docs_paths_keep_the_pair(eng, world, monkeypatch):
    """Every git clone (identity.runs.repo_access: preview, skill sources, repo index, security
    scan), the agent's workspace, the build's clone token (deploy/code task), the outage retry's
    adapter + push, the docs agent's adapter and the read tools: each (token, host) recorded."""
    from boss_engine.identity import lineage, runs
    from boss_engine.services import (agent_loop, agent_shell, code_task_service, docs_agent,
                                      github_outage, repo_host)
    from boss_engine.services.github_adapter import GitHubAdapter
    from boss_engine.services.github_connections import github_credential

    project, settings = world
    git_pairs: list[tuple[str, str]] = []
    api_pairs: list[tuple[str, str]] = []
    real_git_access, real_init = repo_host.git_access, GitHubAdapter.__init__

    def recording_git_access(host, owner, repo, token="", *, web_base):
        git_pairs.append((token or "", web_base))
        return real_git_access(host, owner, repo, token, web_base=web_base)

    def recording_init(self, settings, **kw):
        real_init(self, settings, **kw)
        api_pairs.append((kw.get("token") or "", self.api_base_url))

    monkeypatch.setattr(repo_host, "git_access", recording_git_access)
    monkeypatch.setattr(GitHubAdapter, "__init__", recording_init)
    monkeypatch.setattr(runs, "egress_enabled", lambda: False)
    monkeypatch.setattr(lineage, "current", lambda: None)
    monkeypatch.setattr(lineage, "_enforced", lambda: False)

    for owner, repo in (("platform", "gateway"), ("someone", "public-lib")):
        # 1. a clone through identity.runs.repo_access (preview / skills / index / scan)
        with runs.repo_access(eng, host="github", owner=owner, repo=repo, project_id=project,
                              credential=github_credential(eng, settings, project, owner,
                                                           repo)) as access:
            assert access.url.startswith((GHES_WEB, github_urls.PUBLIC_WEB))
        # 2. the build's clone token (code task and the deploy's clone choose the same way)
        token, api = code_task_service._clone_token(eng, settings, owner=owner, repo=repo,
                                                    project_id=project, repo_host_kind="github")
        api_pairs.append((token, api))
        with eng.begin() as c:
            # 3. the outage retry: adapter + push
            github_outage._default_github(c, settings, project, owner, repo)
            # 4. the docs agent's adapter
            agent = docs_agent.DocsUpdateService.__new__(docs_agent.DocsUpdateService)
            agent.settings = settings
            agent._adapter(c, project, {"owner": owner, "repo": repo})  # noqa: SLF001
            # 5. the agent's shell workspace
            monkeypatch.setattr(agent_loop, "_run_repo_and_branch",
                                lambda *a, **k: {"owner": owner, "repo": repo, "branch": "main",
                                                 "run_id": "r"})
            monkeypatch.setattr(agent_shell, "ensure_workspace", lambda *a, **k: ("/tmp/x", None))
            agent_loop._open_workspace(c, settings, "r", project)  # noqa: SLF001

    api_allowed = ALLOWED | {("", GHES), ("", github_urls.PUBLIC_API)}
    crossed = [p for p in api_pairs if p not in api_allowed] + \
        [p for p in git_pairs if p not in ALLOWED_WEB]
    assert not crossed, f"a token reached another host: {crossed}"
    assert (GHES_TOKEN, GHES_WEB) in git_pairs and (GHES_TOKEN, GHES) in api_pairs
    assert any(w == github_urls.PUBLIC_WEB for _t, w in git_pairs)


def test_an_adapter_without_a_token_never_lends_the_install_credential_to_another_host():
    from boss_engine.config import get_settings
    from boss_engine.services.github_adapter import GitHubAdapter, IntegrationConfigurationError

    adapter = GitHubAdapter(get_settings().model_copy(update={"github_token": ENV_TOKEN}),
                            owner="platform", repo="gateway", api_base_url=GHES)
    with pytest.raises(IntegrationConfigurationError):
        adapter._auth_token()  # noqa: SLF001
    with pytest.raises(IntegrationConfigurationError):
        GitHubAdapter(get_settings(), owner="o", repo="r", api_base_url="")
