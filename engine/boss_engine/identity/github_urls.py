"""EVERY github.com WEB LINK BOSS BUILDS, in one place (the console's twin: apps/web/lib/github-urls.ts).

GitHub puts an account's settings in two different trees:

  a personal account (type "User")          https://github.com/settings/...
  an organisation (type "Organization")     https://github.com/organizations/<org>/settings/...

Guessing the tree from a login is how BOSS sent a personal account to
/organizations/<user>/settings/apps/<slug>/permissions (a 404). So no link here is built from a
login alone: each settings link takes an `Account` (login + type), and the type comes from GitHub's
own API — the App's `owner.type` (manifest conversion, GET /app) and the installation's
`account.type` (GET /app/installations/{id}) — stored on identity_github_app.

A link whose account type is unknown is not guessed: `settings_root()` is None and the caller says
so (or falls back to the App's public page, which works for every account).

`tests/test_github_urls.py` checks every link kind for both account types, and that no other file
spells a github.com settings, organizations, App-install or OAuth path.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, urlencode

WEB_BASE_ENV = "BOSS_GITHUB_WEB_URL"           # github.com; a GitHub Enterprise or test host
USER = "User"
ORGANIZATION = "Organization"


def web_base() -> str:
    return (os.environ.get(WEB_BASE_ENV) or "https://github.com").rstrip("/")


def account_type(raw: object) -> Optional[str]:
    """GitHub's `type` field, normalised: "User", "Organization", or None (unknown). A Bot or an
    Enterprise owns no settings tree BOSS links to."""
    value = str(raw or "").strip().lower()
    if value == "user":
        return USER
    if value in ("organization", "organisation", "org"):
        return ORGANIZATION
    return None


@dataclass(frozen=True)
class Account:
    login: Optional[str]
    type: Optional[str]

    @classmethod
    def of(cls, login: object, kind: object) -> "Account":
        return cls(str(login).strip() if login else None, account_type(kind))

    @property
    def known(self) -> bool:
        return self.type == USER or (self.type == ORGANIZATION and bool(self.login))

    def settings_root(self) -> Optional[str]:
        """This account's settings tree, or None when its type is not known."""
        if self.type == USER:
            return f"{web_base()}/settings"
        if self.type == ORGANIZATION and self.login:
            return f"{web_base()}/organizations/{quote(self.login, safe='')}/settings"
        return None


def _q(path: str, params: Optional[dict[str, str]]) -> str:
    return f"{path}?{urlencode(params)}" if params else path


# ---- the GitHub App -----------------------------------------------------------------------------------

def app_new(owner: Account, *, state: Optional[str] = None) -> Optional[str]:
    """Where the manifest form is POSTed to create the App, under the owner's settings tree."""
    root = owner.settings_root()
    return _q(f"{root}/apps/new", {"state": state} if state else None) if root else None


def app_settings(slug: str, owner: Account) -> Optional[str]:
    root = owner.settings_root()
    return f"{root}/apps/{quote(slug, safe='')}" if root and slug else None


def app_permissions(slug: str, owner: Account) -> Optional[str]:
    """The App's "Permissions & events" page, where Workflows: Read and write is added."""
    base = app_settings(slug, owner)
    return f"{base}/permissions" if base else None


def app_advanced(slug: str, owner: Account) -> Optional[str]:
    """The App's "Advanced" settings page, where its owner presses "Make public" so other
    accounts may install it (a private App installs only on its owner)."""
    base = app_settings(slug, owner)
    return f"{base}/advanced" if base else None


def account_page(login: str) -> Optional[str]:
    """A person's or an organisation's public page on GitHub."""
    return f"{web_base()}/{quote(login, safe='')}" if login else None


def installation_settings(installation_id: object, account: Account) -> Optional[str]:
    """The installation's page in the account it is installed on: repositories, and the
    "Accept new permissions" review after the App asks for more."""
    root = account.settings_root()
    inst = str(installation_id or "").strip()
    return f"{root}/installations/{inst}" if root and inst.isdigit() else None


def app_public(slug: str) -> Optional[str]:
    """The App's public page: the same for every account type."""
    return f"{web_base()}/apps/{quote(slug, safe='')}" if slug else None


def install_app(slug: str, *, state: Optional[str] = None) -> Optional[str]:
    """GitHub's "Install" page for the App (it asks which account and which repositories)."""
    base = app_public(slug)
    return _q(f"{base}/installations/new", {"state": state} if state else None) if base else None


def install_app_on(slug: str, target_id: object = None) -> Optional[str]:
    """GitHub's install page for the App aimed at ONE account (a person or an organisation, by its
    numeric id): "Install BOSS on <org>". Without an id, the plain install page (GitHub asks)."""
    tid = str(target_id or "").strip()
    base = install_app(slug)
    if not base or not tid.isdigit():
        return base
    return f"{base}/permissions?{urlencode({'target_id': tid})}"


# ---- OAuth (a person's own connection; GitHub as a sign-in provider) ------------------------------------

def oauth_authorize(params: Optional[dict[str, str]] = None) -> str:
    return _q(f"{web_base()}/login/oauth/authorize", params)


def oauth_access_token() -> str:
    return f"{web_base()}/login/oauth/access_token"


def device_code() -> str:
    """Device flow, step 1: BOSS asks for a code (no callback URL: it works on any host)."""
    return f"{web_base()}/login/device/code"


def device_verify() -> str:
    """Device flow, step 2: where the person types the code, in any browser."""
    return f"{web_base()}/login/device"


#: github.com itself, for GitHub as an SSO provider (never a GitHub Enterprise host).
OAUTH_AUTHORIZE_PUBLIC = "https://github.com/login/oauth/authorize"
OAUTH_TOKEN_PUBLIC = "https://github.com/login/oauth/access_token"
OAUTH_ISSUER_PUBLIC = "https://github.com/login/oauth"


def oauth_app_new(owner: Optional[Account] = None) -> str:
    """Register an OAuth app: under the person's settings, or an organisation's."""
    root = owner.settings_root() if owner else None
    return f"{root or web_base() + '/settings'}/applications/new"


# ---- tokens -----------------------------------------------------------------------------------------------

def fine_grained_tokens() -> str:
    """The signed-in person's fine-grained tokens (tokens are always a person's, never an org's)."""
    return f"{web_base()}/settings/personal-access-tokens"


def new_fine_grained_token(params: Optional[dict[str, str]] = None) -> str:
    return _q(f"{fine_grained_tokens()}/new", params)


def token_requests(org: Optional[str]) -> str:
    """Where an organisation's owner approves a pending fine-grained token. Without the org's
    name: the person's list of organisations, one click from each org's requests."""
    if org:
        return f"{web_base()}/organizations/{quote(org, safe='')}/settings/personal-access-token-requests"
    return f"{web_base()}/settings/organizations"


# ---- repositories ------------------------------------------------------------------------------------------

def repo(full_name: str) -> str:
    return f"{web_base()}/{full_name.strip('/')}"
