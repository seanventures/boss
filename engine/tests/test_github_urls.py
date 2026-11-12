"""ONE builder for every github.com web link (identity/github_urls.py), and it knows the account type.

A personal account's settings are under /settings/..., an organisation's under
/organizations/<org>/settings/...; a link built from a login alone sent a personal account to an
organisation page (404). Every link kind is checked for both account types here, and a static scan
refuses any other engine/API file that spells one of these paths itself.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from boss_engine.identity import github_urls as G

ROOT = Path(__file__).resolve().parents[2]
USER = G.Account("paramg", G.USER)
ORG = G.Account("acme", G.ORGANIZATION)


@pytest.fixture(autouse=True)
def _github_com(monkeypatch):
    monkeypatch.delenv(G.WEB_BASE_ENV, raising=False)


@pytest.mark.parametrize("account,root", [(USER, "https://github.com/settings"),
                                          (ORG, "https://github.com/organizations/acme/settings")])
def test_every_settings_link_lands_in_the_accounts_own_tree(account, root):
    assert G.app_new(account) == f"{root}/apps/new"
    assert G.app_new(account, state="s.1") == f"{root}/apps/new?state=s.1"
    assert G.app_settings("boss-x", account) == f"{root}/apps/boss-x"
    assert G.app_permissions("boss-x", account) == f"{root}/apps/boss-x/permissions"
    assert G.installation_settings(42, account) == f"{root}/installations/42"
    assert G.oauth_app_new(account) == f"{root}/applications/new"


def test_a_personal_account_never_gets_an_organizations_link():
    for url in (G.app_new(USER), G.app_settings("s", USER), G.app_permissions("s", USER),
                G.installation_settings("7", USER), G.oauth_app_new(USER)):
        assert "/organizations/" not in str(url)
        assert "paramg" not in str(url)            # a personal tree is the signed-in person's


def test_links_that_do_not_depend_on_the_account():
    assert G.app_public("boss-x") == "https://github.com/apps/boss-x"
    assert G.install_app("boss-x") == "https://github.com/apps/boss-x/installations/new"
    assert G.install_app("boss-x", state="a b") == "https://github.com/apps/boss-x/installations/new?state=a+b"
    assert G.oauth_authorize() == "https://github.com/login/oauth/authorize"
    assert G.oauth_authorize({"client_id": "c"}) == "https://github.com/login/oauth/authorize?client_id=c"
    assert G.oauth_access_token() == "https://github.com/login/oauth/access_token"
    assert G.fine_grained_tokens() == "https://github.com/settings/personal-access-tokens"
    assert G.new_fine_grained_token({"name": "BOSS"}) == \
        "https://github.com/settings/personal-access-tokens/new?name=BOSS"
    assert G.token_requests("acme") == \
        "https://github.com/organizations/acme/settings/personal-access-token-requests"
    assert G.token_requests(None) == "https://github.com/settings/organizations"
    assert G.repo("paramg/boss-sandbox") == "https://github.com/paramg/boss-sandbox"
    assert G.oauth_app_new() == "https://github.com/settings/applications/new"


def test_an_unknown_account_type_is_never_guessed():
    for unknown in (G.Account("paramg", None), G.Account(None, G.ORGANIZATION), G.Account.of("x", "Bot")):
        assert not unknown.known
        assert G.app_new(unknown) is None
        assert G.app_permissions("s", unknown) is None
        assert G.installation_settings(1, unknown) is None


def test_account_type_is_read_the_way_github_spells_it():
    assert G.account_type("User") == G.USER and G.account_type("user") == G.USER
    assert G.account_type("Organization") == G.ORGANIZATION
    assert G.account_type("Bot") is None and G.account_type(None) is None
    assert G.Account.of(" acme ", "Organization") == ORG


def test_a_github_enterprise_or_test_host_is_followed(monkeypatch):
    monkeypatch.setenv(G.WEB_BASE_ENV, "https://ghe.example/")
    assert G.app_permissions("s", ORG) == "https://ghe.example/organizations/acme/settings/apps/s/permissions"
    assert G.install_app("s") == "https://ghe.example/apps/s/installations/new"


def test_a_personal_install_link_goes_through_githubs_own_sign_in():
    """BOSSAPP-1015: a person's install page only answers to that person's session (a wrong one is
    a bare 404), so the sure route wraps it in github.com/login?return_to=…."""
    install = G.install_app_on("boss-x", 8303)
    assert G.login_then(install) == ("https://github.com/login?return_to="
                                     "%2Fapps%2Fboss-x%2Finstallations%2Fnew%2Fpermissions%3Ftarget_id%3D8303")
    assert G.login_then(None) is None
    assert G.login_then("https://evil.example/apps/x") is None     # only GitHub's own pages


def test_login_then_follows_a_github_enterprise_host(monkeypatch):
    monkeypatch.setenv(G.WEB_BASE_ENV, "https://ghe.example")
    assert G.login_then("https://ghe.example/apps/s/installations/new") == \
        "https://ghe.example/login?return_to=%2Fapps%2Fs%2Finstallations%2Fnew"
    assert G.login_then("https://github.com/apps/s") is None       # the other host is foreign now


def test_hostile_names_cannot_change_the_path():
    assert G.installation_settings("1/../2", ORG) is None
    assert G.app_permissions("a/b", ORG).endswith("/apps/a%2Fb/permissions")
    assert G.Account("a/b", G.ORGANIZATION).settings_root().endswith("/organizations/a%2Fb/settings")


# ---- nobody else builds these links ----------------------------------------------------------------------

#: A github.com settings tree, an App's install page or the OAuth endpoints, spelled out.
FORBIDDEN = re.compile(r"github\.com/(settings|organizations)\b|/organizations/\{|/settings/apps\b|"
                       r"/settings/installations\b|/installations/new\b|github\.com/login/oauth|"
                       r"/settings/personal-access-tokens\b")
SCANNED = ("engine/boss_engine", "apps/api/app", "apps/worker", "apps/egress")
ALLOWED = {"engine/boss_engine/identity/github_urls.py"}


def test_no_other_file_builds_a_github_settings_or_install_link():
    offenders = []
    for top in SCANNED:
        for path in (ROOT / top).rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if rel in ALLOWED or "/tests/" in rel:
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if FORBIDDEN.search(line) and "api.github.com" not in line:
                    offenders.append(f"{rel}:{n}: {line.strip()[:120]}")
    assert not offenders, "build these with identity/github_urls.py:\n" + "\n".join(offenders)
