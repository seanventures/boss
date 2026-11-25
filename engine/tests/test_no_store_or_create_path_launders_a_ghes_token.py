"""Two paths the fourth review of #330 found (BOSSAPP-1286).

S1 — storage laundering. Setup › select repository mirrored ANY connection's token into the org
token store (setup_actions._mirror_token_into_vault -> github_setup.store_org_token), whose strip()
dropped the host; read back through control_plane_token it became an install credential and went
to github.com. Now: a token of another host is never mirrored, store_org_token refuses one, and
everything read back from the store is the install's by construction.

S2 — repository create/check. local_publish.create_repository/check_repository sent Bearer to the
reach's base without the send check, and repo_creation's candidates picked any PAT whose login
matched the owner on any host. Now: the credential's own host, the send check, and candidates of
the target host only.
"""
from __future__ import annotations

import httpx
import pytest
from sqlalchemy import text

from boss_engine.identity import github_urls as G

from ._ops2_support import fresh_engine, make_project

GHES = "https://ghe.northgate.example/api/v3"


@pytest.fixture(autouse=True)
def _install_is_github_com(monkeypatch):
    monkeypatch.delenv(G.API_BASE_ENV, raising=False)
    monkeypatch.delenv(G.WEB_BASE_ENV, raising=False)


@pytest.fixture(scope="module")
def eng():
    engine, drop = fresh_engine("launder")
    yield engine
    drop()


def _connection(eng, project, cid, base, login="platform"):
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO github_connections (id, project_id, connection_label, auth_method, "
            "credential_ref, status, api_base_url, account_login, created_by) VALUES "
            "(:i, :p, :i, 'personal_access_token', 'env:X', 'active', :b, :l, 'ceo')"),
            {"i": cid, "p": project, "b": base, "l": login})


# ---- S1: the exact chain ----------------------------------------------------------------------

def test_a_ghes_connection_token_is_never_mirrored_into_the_org_store(eng, monkeypatch):
    from boss_engine.identity import github_setup
    from boss_engine.services import setup_actions
    from boss_engine.services.github_connections import GitHubConnectionService
    from boss_engine.config import get_settings

    project = make_project(eng)
    cid = f"ghc_{project}"
    _connection(eng, project, cid, GHES)
    stored = []
    monkeypatch.setattr(github_setup, "store_org_token", lambda **k: stored.append(k))
    monkeypatch.setattr(setup_actions, "_is_app_connection", lambda *a, **k: False)
    monkeypatch.setattr(setup_actions, "_signin_person", lambda *a, **k: False)
    service = GitHubConnectionService(get_settings())
    monkeypatch.setattr(service, "_resolve_secret", lambda *a, **k: "ghes-secret")
    with eng.begin() as c:
        setup_actions._mirror_token_into_vault(c, service, project, cid, "usr_ceo")  # noqa: SLF001
        said = c.execute(text("SELECT data FROM events WHERE project_id = :p AND "
                              "event_type = 'github.org_token_unchanged'"), {"p": project}).scalar()
    assert stored == [], "a GHES token reached the install's org token store"
    assert said and said.get("reason") == "other_host"


def test_the_org_store_refuses_a_token_of_another_host():
    from boss_engine.identity import github_setup

    with pytest.raises(github_setup.GitHubSetupError) as refused:
        github_setup.store_org_token(token=G.tag("ghes-secret", GHES), repos=["platform/gw"],
                                     org_id="org_x", db=None, by="usr_ceo", check=False)
    assert refused.value.code == "token_for_other_host"


def test_what_the_org_store_hands_back_is_tagged_as_the_install(monkeypatch):
    from boss_engine.identity import github_setup, vault

    monkeypatch.setattr(github_setup, "app_config", lambda db, org_id=None: {"source": "env"})
    monkeypatch.setattr(github_setup, "org_token_for", lambda db, repos, org_id=None: {"id": "v1"})
    monkeypatch.setattr(vault, "org_github_token", lambda item, db=None: "org-secret")
    got = github_setup.control_plane_token(None, "acme", "web")
    assert isinstance(got, G.GitHubCredential) and got.api_base == G.PUBLIC_API
    with pytest.raises(G.CrossHostCredential):
        G.check_send(got, GHES)


# ---- S2: create / check a repository ----------------------------------------------------------

def _recording_client(seen):
    def handle(request):
        seen.append(str(request.url))
        return httpx.Response(404, json={})
    return httpx.Client(transport=httpx.MockTransport(handle))


def test_check_and_create_send_to_the_credentials_own_host_only():
    from boss_engine.services import local_publish

    seen: list[str] = []
    out = local_publish.check_repository(owner="platform", name="gw",
                                         token=G.tag("ghes-secret", GHES),
                                         api_base_url=G.PUBLIC_API, client=_recording_client(seen))
    assert seen and all(u.startswith(GHES) for u in seen), seen     # its own host, not the reach's
    seen.clear()
    out = local_publish.check_repository(owner="platform", name="gw", token="install-token",
                                         api_base_url=GHES, client=_recording_client(seen))
    assert out["reason"] == "credential_host_mismatch" and seen == []
    made = local_publish.create_repository(owner="platform", name="gw", token="install-token",
                                           api_base_url=GHES, client=_recording_client(seen))
    assert made.reason == "credential_host_mismatch" and seen == []


def test_a_connection_of_another_host_is_never_a_creation_candidate(eng):
    from boss_engine.config import get_settings
    from boss_engine.services import repo_creation

    project = make_project(eng)
    _connection(eng, project, f"ghes_{project}", GHES, login="platform")
    _connection(eng, project, f"gh_{project}", G.PUBLIC_API, login="platform")
    with eng.begin() as c:
        found = repo_creation._connection_candidates(  # noqa: SLF001
            c, get_settings(), "platform", project, probe=False)
    ids = {getattr(f, "connection_id", None) or getattr(f, "id", None) for f in found}
    assert f"ghes_{project}" not in ids and f"gh_{project}" in ids, ids
