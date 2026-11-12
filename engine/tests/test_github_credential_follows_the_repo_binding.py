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


def _second_account_world(c) -> None:
    """The workspace, the project and account B's token connection (BOSSAPP-1015..1017), without
    the binding: the direct-SQL and the service binds share this."""
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


def _bind_second_account(env, repo: str = REPO_B) -> None:  # noqa: F811
    """Account B the way BOSSAPP-1015..1017 connect it: a project connection holding a token,
    with `repo` bound to it (github.repository_bound)."""
    with env.db() as c:
        _second_account_world(c)
        c.execute(text(
            "INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, full_name, "
            "role, created_by) VALUES ('ghrepo_b', 'prj_web', 'ghconn_b', :o, :n, :f, 'source', 'usr_alice')"),
            {"o": repo.split("/")[0], "n": repo.split("/")[1], "f": repo})


def _registry_world(env) -> None:
    """The org registry's tables beside the binding world: github_repos with its dedupe index,
    the events table the binding service emits through, and the bindings host column — each the
    slice db/schema.sql defines. The identity reset never truncates github_repos (it carries no
    project reference), so the registry starts each test empty here."""
    with env.db() as c:
        _second_account_world(c)
        sql = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
        for table in ("github_repos", "events"):
            start = sql.index(f"CREATE TABLE IF NOT EXISTS {table} (")
            c.execute(text(sql[start:sql.index("\n);", start) + 3]))
        idx = sql.index("CREATE UNIQUE INDEX IF NOT EXISTS github_repos_org_full_name")
        c.execute(text(sql[idx:sql.index(";", idx)]))
        c.execute(text("ALTER TABLE github_repository_bindings ADD COLUMN IF NOT EXISTS host TEXT "
                       "NOT NULL DEFAULT 'github'"))
        c.execute(text("DELETE FROM github_repos"))


def _bind_via_service(env, repo: str = REPO_B) -> dict:
    """Account B's repo bound the way every surface binds one: through the binding service
    (console PATCH, agent tool, setup, easy_connect) — the path that registers the repo."""
    from boss_engine.services.github_connections import GitHubConnectionService

    with env.db() as c:
        return GitHubConnectionService(object()).create_repository_binding(
            c, project_id="prj_web", connection_id="ghconn_b", repository=repo,
            role="source", default_branch=None, metadata={}, actor="usr_alice")


def _registry_row(c, repo: str = REPO_B):
    return c.execute(text(
        "SELECT source, allowed, allowed_by, reachable, archived FROM github_repos "
        "WHERE org_id = 'org_boss' AND lower(full_name) = lower(:f)"), {"f": repo}).mappings().first()


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


# ---- BOSSAPP-1030: binding a repo registers it, so a registry in use lets egress mint for it ----


def test_binding_a_repo_registers_it_and_egress_mints_while_the_registry_is_in_use(env, monkeypatch):  # noqa: F811
    """Found live (12 Nov, third link of the multi-account chain): with the connect flow and the
    credential ladder both fixed, egress still refused the bound repo — repo_not_allowed — because
    the registry (in use: the App account's rows) had never heard of what the bind flow bound.
    The operator explicitly picking the repo IS the allow action: binding registers it."""
    _connect_app(env)                                # account A: the App, installed on acme
    _registry_world(env)
    with env.db() as c:                              # the registry is IN USE: the App account's row
        c.execute(text("INSERT INTO github_repos (id, org_id, source, full_name, allowed, allowed_by, reachable) "
                       "VALUES ('ghr_app_web', 'org_boss', 'app', 'acme/web', true, 'usr_alice', true)"))
    _bind_via_service(env)                           # account B: REPO_B bound through its connection
    monkeypatch.setenv(B_ENV, B_TOKEN)
    with env.db() as c:
        row = _registry_row(c)
        assert row and row["source"] == "binding" and row["allowed"] and row["reachable"]
        assert row["allowed_by"] == "usr_alice"      # the bind is the operator's allow action
    shim, up, proxy = _run(env, repos=(REPO_B,), run="run_ct:reg1")
    resp = _clone(proxy, shim, REPO_B)
    assert resp.status == 200 and _injected(up) == B_TOKEN           # minted and fetched, not refused
    other, up2, proxy2 = _run(env, repos=("acme/web",), run="run_ct:reg2")
    assert _clone(proxy2, other).status == 200
    assert _injected(up2) == env.hub.issued[0]                       # A's repos keep the App path


def test_an_operator_unticked_repo_stays_refused_even_when_bound(env, monkeypatch):  # noqa: F811
    """The registry's documented contract stands: unticked = refused, binding or not."""
    _registry_world(env)
    with env.db() as c:                              # the operator's explicit untick (allowed_by set)
        c.execute(text("INSERT INTO github_repos (id, org_id, source, full_name, allowed, allowed_by, reachable) "
                       "VALUES ('ghr_b_untick', 'org_boss', 'token', :f, false, 'usr_alice', true)"), {"f": REPO_B})
    _bind_via_service(env)
    monkeypatch.setenv(B_ENV, B_TOKEN)
    with env.db() as c:
        row = _registry_row(c)
        assert row["allowed"] is False and row["source"] == "token"  # the bind never overrules the untick
    shim, up, proxy = _run(env, repos=(REPO_B,), run="run_ct:reg3")
    resp = _clone(proxy, shim, REPO_B)
    assert resp.status == 403 and b"repo_not_allowed" in resp.body
    assert up.calls == [] and B_TOKEN.encode() not in resp.body


def test_a_registry_not_in_use_stays_ungoverned_when_a_repo_is_bound(env, monkeypatch):  # noqa: F811
    """An install that never used the registry keeps today's behaviour: the bind registers
    nothing, mint_allowed keeps answering None, and the binding's own credential serves."""
    from boss_engine.multirepo import registry

    _registry_world(env)
    _bind_via_service(env)
    monkeypatch.setenv(B_ENV, B_TOKEN)
    with env.db() as c:
        assert c.execute(text("SELECT count(*) FROM github_repos WHERE org_id = 'org_boss'")).scalar() == 0
        assert registry.mint_allowed(c, "org_boss", [REPO_B]) is None
    shim, up, proxy = _run(env, repos=(REPO_B,), run="run_ct:reg4")
    assert _clone(proxy, shim, REPO_B).status == 200 and _injected(up) == B_TOKEN


def test_deactivating_a_binding_leaves_the_allowed_flag_as_the_operator_set_it(env):  # noqa: F811
    """Unticking disables bindings (registry -> project); deactivating a binding never walks the
    other way. The operator's registry tick stays theirs, and rebinding dedupes onto the row."""
    from boss_engine.services.github_connections import GitHubConnectionService

    _registry_world(env)
    with env.db() as c:
        c.execute(text("INSERT INTO github_repos (id, org_id, source, full_name, allowed, allowed_by, reachable) "
                       "VALUES ('ghr_app_web', 'org_boss', 'app', 'acme/web', true, 'usr_alice', true)"))
    binding = _bind_via_service(env)
    svc = GitHubConnectionService(object())
    with env.db() as c:
        svc.update_repository_binding(c, project_id="prj_web", binding_id=binding["id"], role=None,
                                      status="disabled", default_branch=None, metadata=None, actor="usr_alice")
        row = _registry_row(c)
        assert row["allowed"] and row["allowed_by"] == "usr_alice"   # not silently flipped
        svc.update_repository_binding(c, project_id="prj_web", binding_id=binding["id"], role=None,
                                      status="active", default_branch=None, metadata=None, actor="usr_alice")
        assert c.execute(text("SELECT count(*) FROM github_repos WHERE org_id = 'org_boss' AND "
                              "lower(full_name) = lower(:f)"), {"f": REPO_B}).scalar() == 1   # deduped
        assert _registry_row(c)["allowed"]


def test_refresh_folds_bound_repos_in_and_never_counts_them_gone(env):  # noqa: F811
    """No installation lists a binding's repo, so the refresh lister never writes its row: the
    first refresh grandfathers it like every used repository, and the gone-sweep (which marks
    what GitHub stopped listing) never touches a 'binding' row — even after the binding is
    deactivated, the operator's allow stays theirs."""
    from boss_engine.multirepo import registry
    from boss_engine.services.github_connections import GitHubConnectionService

    _registry_world(env)
    binding = _bind_via_service(env)                 # registry not in use yet: nothing registered
    lister = lambda db, s, o, p: ("app", [{"full_name": "acme/web", "private": True}])  # noqa: E731
    with env.db() as c:
        out = registry.refresh(c, None, "prj_web", lister=lister)
        assert out["first"] is True
        row = _registry_row(c)
        assert row["source"] == "binding" and row["allowed"] and row["reachable"]    # grandfathered
        assert _registry_row(c, "acme/web")["allowed"] is False      # listed but unused: not allowed
        out2 = registry.refresh(c, None, "prj_web", lister=lister)
        assert out2["gone"] == 0 and _registry_row(c)["allowed"]     # never swept gone
        GitHubConnectionService(object()).update_repository_binding(
            c, project_id="prj_web", binding_id=binding["id"], role=None, status="disabled",
            default_branch=None, metadata=None, actor="usr_alice")
        out3 = registry.refresh(c, None, "prj_web", lister=lister)
        row3 = _registry_row(c)
        assert out3["gone"] == 0 and row3["allowed"] and row3["reachable"]           # tick stays the operator's
