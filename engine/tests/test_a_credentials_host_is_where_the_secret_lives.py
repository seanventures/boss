"""A credential's host comes from WHERE THE SECRET LIVES, never from the connection naming it
(BOSSAPP-1286, fifth review of #330).

S-A: a connection with credential_ref env:BOSS_GITHUB_TOKEN and api_base_url
https://evil.example/api/v3 re-tagged the install's token as evil.example, and git_access sent it
there. Now an env reference is always the install's credential: assert_may_send refuses the
foreign base, a connection may not pair them, and git never builds the header for evil.example.

S-B: a GHES connection with its OWN GitHub App could never mint, because the App JWT was tagged
as the install's. The JWT now carries the host its private key was stored for.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import text

from boss_engine.identity import github_urls as G

from ._ops2_support import fresh_engine, make_project

EVIL = "https://evil.example/api/v3"
GHES = "https://ghe.northgate.example/api/v3"


@pytest.fixture(autouse=True)
def _install_is_github_com(monkeypatch):
    monkeypatch.delenv(G.API_BASE_ENV, raising=False)
    monkeypatch.delenv(G.WEB_BASE_ENV, raising=False)
    monkeypatch.setenv("BOSS_GITHUB_TOKEN", "install-token-never-to-evil")


@pytest.fixture(scope="module")
def eng():
    engine, drop = fresh_engine("origin")
    yield engine
    drop()


def test_an_env_reference_is_tagged_as_the_install_whatever_the_connection_says():
    from boss_engine.services.env_refs import CredentialMisdirected
    from boss_engine.services.github_connections import GitHubConnectionService

    tag = GitHubConnectionService._tag_by_origin  # noqa: SLF001
    with pytest.raises(CredentialMisdirected):          # rc6 N-1: before any re-tag
        tag("install-token-never-to-evil", "env:BOSS_GITHUB_TOKEN", EVIL)
    ok = tag("install-token", "env:BOSS_GITHUB_TOKEN", G.PUBLIC_API)
    assert ok.api_base == G.PUBLIC_API
    stored = tag("pasted-for-ghes", {"ref_kind": "secret", "ref_key": "k"}, GHES)
    assert stored.api_base == GHES                      # a secret stored FOR the connection


def test_a_connection_may_not_pair_an_env_reference_with_another_host():
    from boss_engine.services.github_connections import _refuse_env_ref_on_another_host

    with pytest.raises(ValueError):
        _refuse_env_ref_on_another_host("env:BOSS_GITHUB_TOKEN", EVIL)
    _refuse_env_ref_on_another_host("env:BOSS_GITHUB_TOKEN", G.PUBLIC_API)
    _refuse_env_ref_on_another_host("secret:abc", GHES)


def test_an_existing_env_row_on_evil_never_sends_the_install_token_there(eng, monkeypatch):
    """The exact repro, for a row written before the refusal existed."""
    from boss_engine.config import get_settings
    from boss_engine.services import repo_host
    from boss_engine.services.github_connections import GitHubConnectionService

    project = make_project(eng)
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO github_connections (id, project_id, connection_label, auth_method, "
            "credential_ref, status, api_base_url, created_by) VALUES (:i, :p, 'evil', "
            "'personal_access_token', 'env:BOSS_GITHUB_TOKEN', 'active', :b, 'ceo')"),
            {"i": f"ghc_{project}", "p": project, "b": EVIL})
        c.execute(text(
            "INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, "
            "full_name, role, created_by) VALUES (:b, :p, :c, 'acme', 'web', 'acme/web', "
            "'primary', 'ceo')"), {"b": f"ghb_{project}", "p": project, "c": f"ghc_{project}"})
    service = GitHubConnectionService(get_settings())
    monkeypatch.setattr(service, "_resolve_connection_secret",
                        lambda *a, **k: "install-token-never-to-evil")
    with eng.begin() as c:
        try:
            token = service.token_for_repository(c, project_id=project, owner="acme", repo="web")
        except Exception:  # noqa: BLE001 — refused outright is fine too
            token = None
    if token:
        assert isinstance(token, G.GitHubCredential) and token.api_base == G.PUBLIC_API
        with pytest.raises(G.CrossHostCredential):
            repo_host.git_access("github", "acme", "web", token,
                                 web_base=G.web_base_for(EVIL))


def test_a_ghes_connection_with_its_own_app_can_mint(monkeypatch):
    from boss_engine.services.github_adapter import GitHubAdapter
    from boss_engine.services.github_app_adapter import GitHubAppAdapter

    key = G.tag("-----BEGIN PRIVATE KEY----- ghes-app", GHES)
    settings = SimpleNamespace(github_app_private_key=key, github_app_private_key_path=None,
                               github_app_id="9", github_app_installation_id="1",
                               github_token=None, target_github_owner="", target_github_repo="")
    app = GitHubAppAdapter.__new__(GitHubAppAdapter)
    app.settings, app.api_base_url = settings, GHES
    app._check_jwt_host()                        # noqa: SLF001 — its own host: allowed
    install_key = SimpleNamespace(**{**vars(settings), "github_app_private_key": "install-key"})
    app.settings = install_key
    with pytest.raises(G.CrossHostCredential):   # the install's App never mints on GHES
        app._check_jwt_host()  # noqa: SLF001
    # The adapter for that connection asks its own App, not "no credential for this host".
    minted = []
    monkeypatch.setattr("boss_engine.services.github_app_adapter.repo_token",
                        lambda s, o, r, **k: minted.append(k.get("api_base_url"))
                        or G.tag("ghs_ghes_installation", GHES))
    adapter = GitHubAdapter(settings, owner="platform", repo="gw", api_base_url=GHES)
    assert adapter._auth_token().api_base == GHES and minted == [GHES]  # noqa: SLF001
