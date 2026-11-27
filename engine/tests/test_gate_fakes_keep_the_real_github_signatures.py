"""The e2e gates' local GitHub fakes keep the REAL signatures of the seams they stand in for
(repo_host.clone_url_for, GitHubAdapter.__init__), and point BOSS at the local GitHub through the
install's SUPPORTED setting (BOSS_GITHUB_API_URL / BOSS_GITHUB_WEB_URL).

#330 made clone_url_for's ``web_base`` keyword-only and required, and the engines gate's fake
(``def clone_url_for(host, owner, repo, token="")``) failed all 14 builds with "unexpected keyword
argument 'web_base'". The plumbing gate paired an untagged token (the install's, so
api.github.com's) with the local GitHub's base by hand and was refused at the send point.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "e2e" / "gate"))
sys.path.insert(0, str(ROOT / "tests" / "e2e" / "stubs"))


def _accepts(fake, real) -> None:
    """Every keyword the real function takes, the fake takes too (as a keyword)."""
    fake_sig, real_sig = inspect.signature(fake), inspect.signature(real)
    var_kw = any(p.kind is p.VAR_KEYWORD for p in fake_sig.parameters.values())
    for name, param in real_sig.parameters.items():
        if name == "self" or param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        assert var_kw or name in fake_sig.parameters, f"{fake.__qualname__} lacks {name!r}"


def test_the_local_github_fakes_take_the_real_signatures_and_use_the_supported_setting(
        tmp_path, monkeypatch):
    from boss_engine.identity import github_urls as G
    from boss_engine.services import github_adapter, repo_host

    real_clone, real_init = repo_host.clone_url_for, github_adapter.GitHubAdapter.__init__
    monkeypatch.delenv(G.API_BASE_ENV, raising=False)
    monkeypatch.delenv(G.WEB_BASE_ENV, raising=False)
    from remote import LocalGitHub

    hub = LocalGitHub(tmp_path / "github").start()
    try:
        undo = hub.point_boss_at_me()
        try:
            _accepts(repo_host.clone_url_for, real_clone)
            _accepts(github_adapter.GitHubAdapter.__init__, real_init)
            # The install's GitHub is the local one, so the install's own (untagged) token may go
            # there — the same path a GHES-configured install takes.
            assert G.install_api_base() == hub.api_url
            G.check_send(G.tag("gate-local-token", ""), hub.api_url, where="gate")
            assert repo_host.clone_url_for("github", "o", "r", web_base="https://github.com") \
                == hub.git_url("o", "r")
            assert G.tag("gate-local-token", "").api_base == hub.api_url
        finally:
            undo()
        assert repo_host.clone_url_for is real_clone
        assert G.install_api_base() == G.PUBLIC_API
    finally:
        hub.stop()


def test_a_configured_install_base_tags_the_install_token_with_that_base(monkeypatch):
    """The product side: BOSS_GITHUB_API_URL moves the install's GitHub, and the install's token
    is tagged with THAT base, never a hard-coded api.github.com."""
    from types import SimpleNamespace

    from boss_engine.identity import github_urls as G
    from boss_engine.services.github_app_adapter import repo_token

    ghes = "https://ghe.northgate.example/api/v3"
    monkeypatch.setenv(G.API_BASE_ENV, ghes)
    monkeypatch.delenv(G.WEB_BASE_ENV, raising=False)
    app = SimpleNamespace(configured=False)
    settings = SimpleNamespace(github_token="install-token", github_app_private_key=None)
    token = repo_token(settings, "acme", "web", api_base_url=ghes, app=app)
    assert isinstance(token, G.GitHubCredential) and token.api_base == ghes
    G.check_send(token, f"{ghes}/repos/acme/web", where="test")
    G.check_send(token, "https://ghe.northgate.example/acme/web.git", where="test")
