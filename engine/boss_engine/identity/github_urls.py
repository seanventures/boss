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
API_BASE_ENV = "BOSS_GITHUB_API_URL"           # api.github.com; GHES: https://<host>/api/v3
GIT_BASE_ENV = "BOSS_GITHUB_GIT_URL"           # the install's git origin, when not its web origin
USER = "User"
ORGANIZATION = "Organization"

#: The ONLY spelling of GitHub's public hosts in engine/API code (BOSSAPP-1286). Everything else
#: asks this module, so a GitHub Enterprise Server connection is never bypassed by a default.
#: ``tests/test_github_hosts_come_from_one_builder.py`` fails on a new literal anywhere else.
PUBLIC_API = "https://api.github.com"
PUBLIC_WEB = "https://github.com"
PUBLIC_API_HOST = "api.github.com"
PUBLIC_WEB_HOST = "github.com"
#: github.com's own hosts (API, web, uploads, archive downloads, raw files, release assets).
PUBLIC_HOSTS = (PUBLIC_API_HOST, PUBLIC_WEB_HOST, "uploads.github.com", "codeload.github.com",
                "raw.githubusercontent.com", "objects.githubusercontent.com")
#: GitHub Enterprise Server serves REST under /api/v3 and GraphQL at /api/graphql.
GHES_REST_PATH = "/api/v3"
GHES_GRAPHQL_PATH = "/api/graphql"


def web_base() -> str:
    """The install's GitHub web origin: BOSS_GITHUB_WEB_URL, else derived from
    BOSS_GITHUB_API_URL (a GHES ``https://h/api/v3`` -> ``https://h``), else github.com."""
    explicit = (os.environ.get(WEB_BASE_ENV) or "").strip()
    if explicit:
        return explicit.rstrip("/")
    api = (os.environ.get(API_BASE_ENV) or "").strip()
    return web_base_for(api) if api else PUBLIC_WEB


def install_api_base() -> str:
    """The install's GitHub REST base: BOSS_GITHUB_API_URL, else derived from
    BOSS_GITHUB_WEB_URL (a GHES ``https://h`` -> ``https://h/api/v3``), else api.github.com."""
    explicit = (os.environ.get(API_BASE_ENV) or "").strip()
    if explicit:
        return explicit.rstrip("/")
    web = (os.environ.get(WEB_BASE_ENV) or "").strip()
    return api_base_for_web(web) if web else PUBLIC_API


def api_base(configured: object = None) -> str:
    """THE REST base for a GitHub call: the connection's configured ``api_base_url`` when it has
    one, else the install's (env), else api.github.com. Every GitHub call derives from this."""
    value = str(configured or "").strip()
    return value.rstrip("/") if value else install_api_base()


def is_public_api(api: object = None) -> bool:
    return api_base(api) == PUBLIC_API


def graphql_url(api: object = None) -> str:
    """GraphQL next to a REST base: api.github.com/graphql, or <host>/api/graphql on GHES."""
    base = api_base(api)
    if base.endswith(GHES_REST_PATH):
        return base[: -len(GHES_REST_PATH)] + GHES_GRAPHQL_PATH
    return base + "/graphql"


def web_base_for(api: object = None) -> str:
    """The web origin that goes with a REST base (where repositories and PRs are browsed and
    cloned): github.com for the public API, ``https://h`` for ``https://h/api/v3``."""
    base = api_base(api) if api else install_api_base()
    if base == PUBLIC_API:
        return PUBLIC_WEB
    if base.endswith(GHES_REST_PATH):
        return base[: -len(GHES_REST_PATH)]
    return base


def api_base_for_web(web: object) -> str:
    """The REST base for a web origin: api.github.com for github.com, else ``<origin>/api/v3``."""
    origin = str(web or "").strip().rstrip("/")
    if not origin or origin == PUBLIC_WEB:
        return PUBLIC_API
    return origin if origin.endswith(GHES_REST_PATH) else origin + GHES_REST_PATH


def web_host(api: object = None) -> str:
    """The web host (no scheme) for a REST base: ``github.com`` or the GHES host."""
    from urllib.parse import urlsplit

    return (urlsplit(web_base_for(api)).hostname or PUBLIC_WEB_HOST).lower()


def github_hosts(api: object = None) -> tuple[str, ...]:
    """Every host a GitHub connection may reach: github.com's own, plus a GHES host."""
    from urllib.parse import urlsplit

    extra = []
    for url in (api_base(api), web_base_for(api)):
        host = (urlsplit(url).hostname or "").lower()
        if host and host not in PUBLIC_HOSTS:
            extra.append(host)
    return PUBLIC_HOSTS + tuple(dict.fromkeys(extra))


# ---- a credential knows its host; a send to another host is refused (BOSSAPP-1286) ---------------

class GitHubCredential(str):
    """A GitHub token that KNOWS THE HOST it belongs to.

    Every token source returns one (the one resolver, a connection's token, the App's, the
    install's): ``api_base`` is the REST base of the GitHub it was issued by (api.github.com, or
    a GHES ``https://<host>/api/v3``). It is a ``str`` so every existing consumer keeps working;
    the SEND points (HTTP transports, git credential injection) call :func:`check_send` and refuse
    to put it on the wire to any other host. ``repr`` never shows the token."""

    api_base: str

    def __new__(cls, token: object, api_base: object):
        obj = super().__new__(cls, str(token or ""))
        obj.api_base = api_base_of(api_base)
        return obj

    @property
    def web_base(self) -> str:
        return web_base_for(self.api_base)

    def __repr__(self) -> str:
        return f"GitHubCredential(<{len(self)} chars>, api_base={self.api_base!r})"

    def __reduce__(self):
        return (GitHubCredential, (str(self), self.api_base))


def api_base_of(value: object) -> str:
    value = str(value or "").strip()
    return value.rstrip("/") if value else install_api_base()


#: sha256 digests of the install's own GitHub credentials (Settings.github_token — from the
#: environment or the .env file — and anything else registered as the install's). An UNTAGGED
#: copy of one (a str() or strip() dropped its tag) can never be tagged for another host.
_INSTALL_DIGESTS: set[str] = set()
_INSTALL_ENV_NAMES = ("BOSS_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")


def _digest(value: object) -> str:
    import hashlib

    return hashlib.sha256(str(value).strip().encode("utf-8")).hexdigest()


def register_install_credential(value: object) -> None:
    """Record a value as one of the install's own GitHub credentials (never stores the value)."""
    if value:
        _INSTALL_DIGESTS.add(_digest(value))


def is_install_credential(value: object) -> bool:
    if not value:
        return False
    d = _digest(value)
    if d in _INSTALL_DIGESTS:
        return True
    return any(os.environ.get(n) and _digest(os.environ[n]) == d for n in _INSTALL_ENV_NAMES)


def tag(token: object, api_base: object) -> "GitHubCredential | str":
    """``token`` tagged with its host ("" stays ""). Refused (never re-labelled): a token
    already tagged for ANOTHER host, and an UNTAGGED copy of one of the install's own
    credentials aimed at any host but the install's — a str()/strip() that dropped the tag must
    not let it be laundered onto a connection's host (BOSSAPP-1286)."""
    if not token:
        return ""
    base = api_base_of(api_base)
    if isinstance(token, GitHubCredential):
        if token.api_base != base:
            _refuse(token, base, "re-tag")
    elif base != install_api_base() and is_install_credential(token):
        _refuse(GitHubCredential(token, install_api_base()), base, "re-tag install credential")
    return GitHubCredential(token, base)


def credential_api_base(token: object) -> str:
    """The REST base a token belongs to: its tag, else the INSTALL's (an untagged token is an
    install/env token by construction; it never belongs to another host)."""
    return token.api_base if isinstance(token, GitHubCredential) else install_api_base()


def _netloc(url: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(url if "://" in url else f"https://{url}")
    host = (parts.hostname or "").lower().rstrip(".")
    return f"{host}:{parts.port}" if parts.port else host


def credential_hosts(api: object) -> frozenset[str]:
    """The hosts a credential of REST base ``api`` may be sent to: github.com's own hosts for the
    public API, else exactly the GHES API host and its web host (with their ports)."""
    base = api_base_of(api)
    hosts = set(PUBLIC_HOSTS) if base == PUBLIC_API else {_netloc(base), _netloc(web_base_for(base))}
    if base == install_api_base():
        # The install's own GitHub as the operator configured it: its web origin
        # (BOSS_GITHUB_WEB_URL) and its explicit git origin (BOSS_GITHUB_GIT_URL, IDENTITY_ADMIN.md).
        hosts.add(_netloc(web_base()))
        git = (os.environ.get(GIT_BASE_ENV) or "").strip()
        if git:
            hosts.add(_netloc(git))
    return frozenset(hosts)


class CrossHostCredential(PermissionError):
    """A GitHub credential was about to be sent to a host it does not belong to."""


def _refuse(token: object, target: str, where: str) -> None:
    import logging

    logging.getLogger("boss.github.credentials").warning(
        "github.cross_host_refused where=%s token_host=%s target=%s", where,
        _netloc(credential_api_base(token)), target)
    # The warning IS the audit record (logger boss.github.credentials, collected with the API's
    # logs); it names both hosts and never the token.
    raise CrossHostCredential(
        f"refused to send a GitHub credential for {_netloc(credential_api_base(token))} to "
        f"{target} ({where})")


class UntaggedGitHubCredential(CrossHostCredential):
    """Strict mode: a credential without a host reached a send point. Every source tags what it
    hands out (a connection's/binding's token, the App's, the org store's, the install's), so an
    untagged one is a source that forgot — a hard error in tests and development."""


#: Untagged credentials that reached a send point, by send point (production keeps sending them
#: as the install's own and counts them; strict mode refuses them).
UNTAGGED_SENDS: "dict[str, int]" = {}
STRICT_ENV = "BOSS_GITHUB_CREDENTIALS_STRICT"


def strict() -> bool:
    return os.environ.get(STRICT_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _untagged(token: object, target: str, where: str) -> None:
    import logging

    UNTAGGED_SENDS[where] = UNTAGGED_SENDS.get(where, 0) + 1
    logging.getLogger("boss.github.credentials").warning(
        "github.untagged_send where=%s target=%s count=%d", where, target, UNTAGGED_SENDS[where])
    if strict():
        raise UntaggedGitHubCredential(
            f"an untagged GitHub credential reached {where} (to {target}); its source must tag "
            "it with its host (github_urls.tag)")


def check_send(token: object, url: object, *, where: str = "http") -> None:
    """THE send-point check. Call it wherever a GitHub token is put on the wire (an HTTP request,
    a git credential header): raises :class:`CrossHostCredential` (and logs/audits it) when the
    target host is not one the token belongs to. An empty token passes."""
    if not token:
        return
    target = _netloc(str(url or ""))
    if target not in credential_hosts(credential_api_base(token)):
        _refuse(token, target, where)
    if not isinstance(token, GitHubCredential):
        _untagged(token, target, where)


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


def login_then(url: Optional[str]) -> Optional[str]:
    """GitHub's sign-in page, going on to `url` after (BOSSAPP-1015). A personal account's install
    page only answers to that very person's session — a wrong session gets a bare 404, not 403 —
    so the sure route is through GitHub's own sign-in, which asks for the right account and
    returns. Only a URL on GitHub itself is wrapped; anything else is None, never guessed."""
    base = web_base()
    if not url or not url.startswith(f"{base}/"):
        return None
    return f"{base}/login?{urlencode({'return_to': url[len(base):]})}"


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
OAUTH_USER_PUBLIC = f"{PUBLIC_API}/user"
OAUTH_EMAILS_PUBLIC = f"{PUBLIC_API}/user/emails"


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

def repo(full_name: str, api: object = None) -> str:
    """A repository's web page; with ``api`` (a connection's REST base), on that connection's
    host (GHES), else on the install's."""
    base = web_base_for(api) if api else web_base()
    return f"{base}/{full_name.strip('/')}"


def clone_url(full_name: str, api: object = None) -> str:
    return f"{repo(full_name, api)}.git"


def pull_request(full_name: str, number: object, api: object = None) -> str:
    return f"{repo(full_name, api)}/pull/{number}"
