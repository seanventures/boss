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
