"""An installation token carries what the repository's CI decision needs (D2, crun_4eec9642).

Minting always narrowed tokens to contents, issues and pull requests. With "Builds may change
CI workflows" on and `workflows: write` accepted on the installation, a finished build's push of
.github/workflows/order-api.yml was refused 403, and the same token could not read CI.
"""
from __future__ import annotations

from boss_engine.services import github_app_adapter as gaa


class FakeApp:
    configured = True

    def __init__(self):
        self.asked = []

    def usable_token_for_repo(self, owner, repo, *, extra=None):
        self.asked.append(dict(extra or {}))
        return "ghs_x", []


class Bindings:
    """A connection whose github_repository_bindings holds the opted-in repositories."""

    def __init__(self, opted):
        self.opted, self.queries = {o.lower() for o in opted}, []

    def begin_nested(self):
        class P:
            def commit(self): pass
            def rollback(self): pass
        return P()

    def execute(self, query, params):
        self.queries.append(str(query))
        hit = params["r"].lower() in self.opted

        class R:
            def first(self): return (1,) if hit else None
        return R()


def test_ci_reads_always_and_workflows_only_with_the_opt_in():
    c = Bindings(["acme/on"])
    assert gaa.scope_for_repo("acme", "on", c) == {**gaa.CI_READ_SCOPES, "workflows": "write"}
    assert gaa.scope_for_repo("acme", "off", c) == gaa.CI_READ_SCOPES
    assert "ci_workflows_write" in c.queries[0] and "status = 'active'" in c.queries[0]


def test_repo_token_asks_for_that_scope(monkeypatch):
    app = FakeApp()
    monkeypatch.setattr(gaa, "_workflows_opted_in", lambda full, connection=None: True)
    assert gaa.repo_token(object(), "acme", "shop", app=app) == "ghs_x"
    assert app.asked == [{**gaa.CI_READ_SCOPES, "workflows": "write"}]


def test_an_unreadable_opt_in_never_widens(monkeypatch):
    class Broken:
        def begin_nested(self):
            raise RuntimeError("db down")
    assert gaa._workflows_opted_in("acme/shop", Broken()) is False


def test_the_setup_apps_push_token_carries_the_opt_in(monkeypatch):
    """crun_4eec9642: the Setup App's control-plane token (the one that pushes a finished build)
    was minted from a fixed set without workflows, whatever the repository's opt-in said."""
    from boss_engine.identity import github_setup as gs

    monkeypatch.setattr(gaa, "_workflows_opted_in", lambda full, connection=None: full == "acme/on")
    assert gs.control_plane_permissions(None, "acme/on") == {
        **gs.CONTROL_PLANE_PERMISSIONS, "workflows": "write"}
    assert gs.control_plane_permissions(None, "acme/off") == gs.CONTROL_PLANE_PERMISSIONS
    asked = []
    monkeypatch.setattr(gs, "app_config", lambda db: {"source": "setup"})
    monkeypatch.setattr(gs, "app_ready", lambda db: True)
    monkeypatch.setattr(gs.github_app, "mint_installation_token",
                        lambda repos, perms, db=None: asked.append(dict(perms)) or
                        type("T", (), {"token": "ghs_x"})())
    monkeypatch.setattr(gs, "_account_for", lambda tok: None)
    monkeypatch.setattr(gs, "replace", lambda tok, **k: tok)
    assert gs.control_plane_token(None, "acme", "on") == "ghs_x"
    assert asked[-1]["workflows"] == "write"


def test_a_held_delivery_says_what_the_token_asked_for_and_what_github_grants():
    """The recovery agent gets the facts to compare, not a 403 alone."""
    from boss_engine.services import governed_code_task_service as g

    words = g._perm_words({"workflows": "write", "contents": "write"})
    assert words == "contents: write, workflows: write"
    assert g._perm_words(None) == "nothing recorded"
    src = __import__("inspect").getsource(g.hold_delivery)
    assert "delivery_credential_facts" in src and '"credential": credential' in src


def test_a_setup_app_binding_pushes_with_its_own_repositorys_token(monkeypatch):
    """crun_4eec9642, third 403: the factory's adapter pushed with the connection's fine-grained
    PAT (no Workflows permission) while the Setup App, which grants workflows, was connected.
    The App's one-repository token comes first; the connection's credential is the fallback."""
    from boss_engine.identity import github_setup
    from boss_engine.services import github_connections as gc

    svc = gc.GitHubConnectionService.__new__(gc.GitHubConnectionService)
    svc._custom_secret_resolver = None
    svc._credential_ref = lambda c, p, i: "database:PROJECT_GITHUB_PAT"
    svc.secret_refs = type("S", (), {"require_usable": lambda self, c, **k: {"ref": "x"}})()
    svc._resolve_secret = lambda ref, c: "wide-all-repos-token"
    svc.settings = type("Settings", (), {"model_copy": lambda self, update: self})()
    asked = []
    monkeypatch.setattr(github_setup, "control_plane_token",
                        lambda db, owner, repo: asked.append((owner, repo)) or "one-repo-token")

    class Conn:
        def begin_nested(self):
            return __import__("contextlib").nullcontext()

    made = {}
    svc.adapter_factory = lambda settings, **k: made.update(k) or k
    svc._make_adapter(Conn(), project_id="p", github_connection={
        "id": "c1", "auth_method": "pat", "app_id": None, "installation_id": None,
        "api_base_url": "https://api.github.com"},
        binding={"full_name": "acme/shop", "owner": "acme", "name": "shop"})
    assert asked == [("acme", "shop")]
    assert made.get("token") == "one-repo-token", made


def test_without_a_setup_app_the_connection_credential_is_used(monkeypatch):
    from boss_engine.identity import github_setup
    from boss_engine.services import github_connections as gc

    svc = gc.GitHubConnectionService.__new__(gc.GitHubConnectionService)
    svc._custom_secret_resolver = None
    svc._credential_ref = lambda c, p, i: "database:PROJECT_GITHUB_PAT"
    svc.secret_refs = type("S", (), {"require_usable": lambda self, c, **k: {"ref": "x"}})()
    svc._resolve_secret = lambda ref, c: "pat-token"
    svc.settings = object()
    monkeypatch.setattr(github_setup, "control_plane_token", lambda db, owner, repo: None)

    class Conn:
        def begin_nested(self):
            return __import__("contextlib").nullcontext()

    made = {}
    svc.adapter_factory = lambda settings, **k: made.update(k) or k
    svc._make_adapter(Conn(), project_id="p", github_connection={
        "id": "c1", "auth_method": "personal_access_token", "app_id": None,
        "installation_id": None, "api_base_url": "https://api.github.com"},
        binding={"full_name": "acme/shop", "owner": "acme", "name": "shop"})
    assert made["token"] == "pat-token"
