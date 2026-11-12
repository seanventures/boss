"""BOSSAPP-1027: WHICH GitHub credential a run gets follows the REPOSITORY'S OWN linkage, never
"an App installation exists". Found live minutes after an operator connected a second GitHub
account: the repo's own connection (personal_access_token, access check PASSED, the repo bound to
it) was never used, because `app_ready` short-circuited the ladder and the KG indexer's fetch
403'd at egress. The ladder now resolves: as-me -> the repo's binding -> an App installation that
COVERS the repos -> a covering org token -> the honest refusal. Proved through egress, as every
credential path is (test_identity_github_setup)."""
from __future__ import annotations

from sqlalchemy import text

from boss_engine.identity import github_setup

from ._identity_agents_support import ROOT
from .test_identity_github_setup import PAT, _clone, _connect_app, _injected, _run, env  # noqa: F401 — the fixture

B_TOKEN = "github_pat_SECONDACCOUNTBOUND00000000000"
B_ENV = "BOSS_KAAPPU_GITHUB_TOKEN"
REPO_B = "paramg-kaapppu/kaappu-org"


def _registry_tables(c) -> None:
    """The project repo registry's tables, as db/schema.sql defines them (the identity test
    schema carries only the identity block)."""
    sql = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    for table in ("secret_refs", "github_connections", "github_repository_bindings"):
        start = sql.index(f"CREATE TABLE IF NOT EXISTS {table} (")
        c.execute(text(sql[start:sql.index("\n);", start) + 3]))


def _bind_second_account(env, repo: str = REPO_B) -> None:  # noqa: F811
    """Account B the way BOSSAPP-1015..1017 connect it: a project connection holding a token,
    with `repo` bound to it (github.repository_bound)."""
    with env.db() as c:
        _registry_tables(c)
        cols = {str(r[0]) for r in c.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'workspaces'"))}
        if "name" in cols:      # the full schema (db.ensure_schema); the identity stubs have no name
            c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_boss', 'org_boss', 'BOSS') "
                           "ON CONFLICT DO NOTHING"))
            c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                           "('prj_web', 'ws_boss', 'prj_web', 'prj-web') ON CONFLICT DO NOTHING"))
        else:
            c.execute(text("INSERT INTO workspaces (id, org_id) VALUES ('ws_boss', 'org_boss') ON CONFLICT DO NOTHING"))
            c.execute(text("INSERT INTO projects (id, workspace_id) VALUES ('prj_web', 'ws_boss') ON CONFLICT DO NOTHING"))
        c.execute(text(
            "INSERT INTO github_connections (id, project_id, connection_label, auth_method, credential_ref, "
            "status, account_login, created_by) VALUES ('ghconn_b', 'prj_web', 'Kaappu org', "
            "'personal_access_token', :ref, 'active', 'paramg-kaapppu', 'usr_alice')"), {"ref": f"env:{B_ENV}"})
        c.execute(text(
            "INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, full_name, "
            "role, created_by) VALUES ('ghrepo_b', 'prj_web', 'ghconn_b', :o, :n, :f, 'source', 'usr_alice')"),
            {"o": repo.split("/")[0], "n": repo.split("/")[1], "f": repo})


def _minted(env, since: int) -> bool:  # noqa: F811
    return any(u.endswith("/access_tokens") for _, u, _, _ in env.hub.calls[since:])


def test_a_repo_bound_to_the_second_accounts_token_uses_it_and_the_app_keeps_its_own(env, monkeypatch):  # noqa: F811
    _connect_app(env)                                # account A: the App, installed on acme
    _bind_second_account(env)                        # account B: a token connection, REPO_B bound to it
    monkeypatch.setenv(B_ENV, B_TOKEN)
    shim, up, proxy = _run(env, repos=(REPO_B,), run="run_ct:bind1")
    before = len(env.hub.calls)
    resp = _clone(proxy, shim, REPO_B)
    assert resp.status == 200 and _injected(up) == B_TOKEN          # the binding's own credential
    assert not _minted(env, before)                                 # never a doomed installation mint
    other, up2, proxy2 = _run(env, repos=("acme/web",), run="run_ct:bind2")
    assert _clone(proxy2, other).status == 200
    assert _injected(up2) == env.hub.issued[0]                      # A's repos keep the App path


def test_the_app_never_arbitrates_a_repo_it_does_not_cover(env):  # noqa: F811
    _connect_app(env)
    shim, up, proxy = _run(env, repos=(REPO_B,), run="run_ct:bind3")
    before = len(env.hub.calls)
    resp = _clone(proxy, shim, REPO_B)
    assert resp.status == 403 and b"app_not_installed" in resp.body
    assert up.calls == [] and not _minted(env, before)
    assert B_TOKEN.encode() not in resp.body and PAT.encode() not in resp.body   # metadata only


def test_an_org_token_covering_the_repo_wins_over_an_app_that_does_not(env):  # noqa: F811
    _connect_app(env)
    with env.db() as c:
        github_setup.store_org_token(token=B_TOKEN, repos=[REPO_B], org_id="org_boss", db=c,
                                     by=env.alice, check=False, keep_others=True, login="paramg-kaapppu")
    shim, up, proxy = _run(env, repos=(REPO_B,), run="run_ct:bind4")
    assert _clone(proxy, shim, REPO_B).status == 200 and _injected(up) == B_TOKEN
    other, up2, proxy2 = _run(env, repos=("acme/web",), run="run_ct:bind5")
    assert _clone(proxy2, other).status == 200
    assert _injected(up2) == env.hub.issued[0]                      # the App still serves what it covers


def test_a_project_connections_token_serves_only_repos_bound_to_it(env, monkeypatch):  # noqa: F811
    _bind_second_account(env)                        # no App, no org token
    monkeypatch.setenv(B_ENV, B_TOKEN)
    shim, up, proxy = _run(env, repos=("paramg-kaapppu/another",), run="run_ct:bind6")
    resp = _clone(proxy, shim, "paramg-kaapppu/another")
    assert resp.status == 403 and b"github_not_connected" in resp.body          # never an org-wide fallback
    assert up.calls == [] and B_TOKEN.encode() not in resp.body
    bound, up2, proxy2 = _run(env, repos=(REPO_B,), run="run_ct:bind7")
    assert _clone(proxy2, bound, REPO_B).status == 200 and _injected(up2) == B_TOKEN
