"""ADD A GITHUB ACCOUNT (BOSSAPP-945; wireframe docs/product/wireframes/agentic-factory/
4-add-github-account.html): one input, the account a person types, and the path BOSS picks for it,
from what GitHub itself says about that account and about this install's GitHub App.

  lookup   the name or a github.com link -> GitHub's own record of the account (GET /users/{name}:
           login, User or Organization, numeric id, avatar, public repositories), whether an
           installation of the App already covers it (GET /app/installations, asked with the App's
           key), and whether a GitHub connection BOSS already holds is this account.
  path     as data:
             pick         the App is installed there already: straight to its repositories;
             make_public  the App is private (or, for a foreign personal target, GitHub would not
                          confirm it is public) and the account is not its owner: GitHub installs
                          a private App only on its owner, and the install page for anyone else is
                          a bare 404. The sheet leads with the direct connection (sign-in or a
                          token) and keeps the owner's "Make public" switch (the App's Advanced
                          settings) behind a note; BOSS still watches the App's public page and
                          offers the app route the moment it opens;
             install      otherwise: GitHub's install page aimed at this account
                          (github_urls.install_app_on with its id; a personal target goes through
                          github.com/login so GitHub asks for the right session first);
             no_app       this install has no Setup-created App yet (the other ways stay).
  watch    the sheet's poll: the App's public page and installations again; an installation for
           the account is recorded (github_setup.record_installation) the moment GitHub lists it.
  repos    that installation's repositories, each with the projects that already use it.
  allow    the registry's allow (multirepo/registry.allow) for the picked ones, which runs the
           access check; each result in words.

Nothing here creates, changes or installs anything on GitHub: every call to GitHub is a read.
"""
from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

from . import github_app, github_setup, github_urls
from .github_setup import GitHubSetupError

#: GitHub's rule for a user or organisation name: letters, digits and single hyphens, 39 at most.
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
#: github.com pages that are not an account.
_NOT_ACCOUNTS = frozenset({"settings", "orgs", "organizations", "apps", "marketplace", "login", "about",
                           "features", "topics", "explore", "notifications", "pulls", "issues", "sponsors"})

PATHS = ("pick", "make_public", "install", "no_app")


# ---- the one input ----------------------------------------------------------------------------------

def parse_account(raw: str) -> str:
    """The account name from what a person typed: `kaappu`, `@kaappu`, `github.com/kaappu`,
    `https://github.com/kaappu/helpdesk` (a repository link names its owner)."""
    text = (raw or "").strip()
    if not text:
        raise GitHubSetupError("account_required", 422, message="Type a GitHub user or organisation name.")
    if "/" in text or text.lower().startswith(("http", "www.", "github.com")):
        url = text if "://" in text else f"https://{text}"
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        web_host = (urlsplit(github_urls.web_base()).hostname or "github.com").lower()
        if host not in ("github.com", "www.github.com", web_host):
            raise GitHubSetupError("not_a_github_link", 422,
                                   message="That link is not on GitHub. Type the account name, or paste "
                                           "its github.com link.")
        segments = [unquote(s) for s in parts.path.split("/") if s]
        if segments and segments[0].lower() in ("orgs", "organizations") and len(segments) > 1:
            segments = segments[1:]
        if not segments or segments[0].lower() in _NOT_ACCOUNTS:
            raise GitHubSetupError("not_an_account_link", 422,
                                   message="That link does not name an account. Paste github.com/<name>.")
        text = segments[0]
    text = text.lstrip("@")
    if not _LOGIN.match(text):
        raise GitHubSetupError("bad_account_name", 422,
                               message=f"“{text[:60]}” is not a GitHub name: letters, digits and single "
                                       "hyphens, 39 at most.")
    return text


# ---- what GitHub says ------------------------------------------------------------------------------------

def _read_token(db: Any) -> str:
    """A token for GitHub's public reads: the App's own installation token when there is one
    (5,000 reads an hour instead of 60), else none (the public API, anonymous)."""
    try:
        if github_setup.app_config(db).get("source") == "setup" and github_setup.app_ready(db):
            return github_setup.installation_token_all(db)
    except Exception:  # noqa: BLE001 — the App cannot mint now: the anonymous public API
        pass
    return ""


def github_account(db: Any, login: str) -> dict[str, Any]:
    token = _read_token(db)
    resp = github_app._call("GET", f"/users/{login}", token=token)  # noqa: SLF001
    if resp.status == 401 and token:
        resp = github_app._call("GET", f"/users/{login}", token="")  # noqa: SLF001
    if resp.status == 404:
        raise GitHubSetupError("github_account_not_found", 404, login=login,
                               message=f"No GitHub account called {login}. Check the spelling.")
    if resp.status in (403, 429):
        raise GitHubSetupError("github_rate_limited", 503,
                               message="GitHub is limiting lookups right now. Try again in a minute.")
    if resp.status != 200:
        raise GitHubSetupError("github_unreachable", 502, status=resp.status,
                               message="GitHub did not answer. Try again in a minute.")
    data = resp.json() or {}
    kind = github_urls.account_type(data.get("type"))
    if kind is None:
        raise GitHubSetupError("github_account_kind", 422, login=login,
                               message=f"{login} is not a person or an organisation on GitHub.")
    real = str(data.get("login") or login)
    return {"login": real, "type": kind, "id": str(data.get("id") or ""), "name": data.get("name"),
            "avatar_url": data.get("avatar_url"), "html_url": data.get("html_url") or github_urls.account_page(real),
            "public_repos": data.get("public_repos")}


def app_is_public(slug: str) -> Optional[bool]:
    """Whether other accounts may install the App. Only the REST API tells the truth here:
    GET /apps/{slug} answers 200 for a public App and 404 for a private one, while the HTML page
    github.com/apps/<slug> serves 200 EITHER WAY — probing it called a private App public and
    aimed a second account at an install page GitHub never shows it (BOSSAPP-1017). None: GitHub
    did not say."""
    if not slug:
        return None
    try:
        resp = github_app._call("GET", f"/apps/{slug}", token="")  # noqa: SLF001 — anonymous public read
    except Exception:  # noqa: BLE001 — unreachable: not known
        return None
    if resp.status == 200:
        return True
    if resp.status == 404:
        return False
    return None


def app_installations(db: Any) -> list[dict[str, Any]]:
    """Every installation of the App, as GitHub lists them to the App itself."""
    jwt = github_app.app_jwt(db)
    out: list[dict[str, Any]] = []
    for page in range(1, 6):
        resp = github_app._call("GET", f"/app/installations?per_page=100&page={page}", token=jwt)  # noqa: SLF001
        if resp.status != 200:
            raise GitHubSetupError("installations_unavailable", 502, status=resp.status,
                                   message="GitHub did not list the BOSS app's installations. Trying again.")
        items = [i for i in (resp.json() or []) if isinstance(i, dict)]
        out.extend(items)
        if len(items) < 100:
            break
    return out


def _installation_for(installations: list[dict[str, Any]], account: dict[str, Any]) -> Optional[dict[str, Any]]:
    for inst in installations:
        who = inst.get("account") or {}
        same_id = account.get("id") and str(who.get("id") or "") == str(account["id"])
        same_login = str(who.get("login") or "").lower() == str(account.get("login") or "").lower()
        if (same_id or same_login) and not inst.get("suspended_at"):
            return inst
    return None


def _connection(db: Any, org_id: str, person_id: str, login: str) -> dict[str, Any]:
    """Is this account one BOSS already holds a GitHub connection for: the person's own sign-in,
    a project's token, or an org token?"""
    from sqlalchemy import text

    from . import github_device

    me = None
    try:
        me = github_device.my_connection(person_id, db=db) if person_id else None
    except Exception:  # noqa: BLE001 — no sign-in table here: not connected that way
        me = None
    if me and str(me.get("login") or "").lower() == login.lower() and me.get("status") == "active":
        return {"covered": True, "how": "signin", "words": "Your GitHub sign-in is this account."}

    def tokens() -> Optional[str]:
        row = db.execute(text(
            "SELECT c.connection_label FROM github_connections c JOIN projects p ON p.id = c.project_id "
            "JOIN workspaces w ON w.id = p.workspace_id WHERE w.org_id = :o AND lower(c.account_login) = lower(:l) "
            "AND c.status NOT IN ('disabled', 'revoked') LIMIT 1"), {"o": org_id, "l": login}).first()
        return "token" if row is not None else None
    how = github_setup._savepoint(db, tokens, None)  # noqa: SLF001
    if how is None:
        for tok in github_setup.org_tokens(db, org_id) if org_id else []:
            if str(tok.get("login") or "").lower() == login.lower():
                how = "token"
                break
    if how:
        return {"covered": True, "how": how, "words": "BOSS already holds an access token for this account."}
    return {"covered": False, "how": None, "words": "You are not connected to it yet."}


def _app(db: Any, org_id: str) -> Optional[dict[str, Any]]:
    cfg = github_setup.app_config(db, org_id)
    if cfg.get("source") != "setup" or not cfg.get("app_id") or not cfg.get("slug"):
        return None
    owner = github_setup.owner_account(cfg)
    return {"slug": cfg["slug"], "owner_login": cfg.get("owner_login"), "owner_type": cfg.get("owner_type"),
            "page_url": github_urls.app_public(cfg["slug"]),
            "settings_url": github_urls.app_advanced(cfg["slug"], owner) or github_urls.app_public(cfg["slug"])}


def _line(path: str, account: dict[str, Any], app: Optional[dict[str, Any]]) -> str:
    login = account["login"]
    if path == "pick":
        return f"The BOSS app is already installed on {login}: pick the repositories BOSS may use."
    if path == "make_public":
        owner = (app or {}).get("owner_login") or "its owner"
        if (app or {}).get("public") is False:
            return (f"The BOSS app is private, so GitHub only installs it on @{owner} — the sure way "
                    f"in for {login} is to connect it directly.")
        return (f"GitHub would not say whether the BOSS app can install on {login}, so the sure way "
                "in is to connect it directly.")
    if path == "install":
        return f"The fastest way for this account: the BOSS app you already own, installed on {login}."
    return "This install has no BOSS GitHub app yet. Create it first, or use another way below."


def plan(db: Any, org_id: str, account: dict[str, Any], *, by: str = "",
         public: Optional[bool] = None) -> dict[str, Any]:
    """The path for one account, from the App's installations and its public page. An
    installation found for the account is recorded on the spot."""
    app = _app(db, org_id)
    if app is None:
        return {"path": "no_app", "app": None, "installation": None, "line": _line("no_app", account, None)}
    found = _installation_for(app_installations(db), account)
    if found is not None:
        kept = github_setup.record_installation(db, org_id, found, by=by)
        on = github_urls.Account.of(account["login"], account.get("type") or kept["account_type"])
        return {"path": "pick", "app": {**app, "public": public},
                "installation": {"installation_id": kept["installation_id"], "new": kept["new"],
                                 "repository_selection": found.get("repository_selection"),
                                 "settings_url": github_urls.installation_settings(kept["installation_id"], on)},
                "line": _line("pick", account, app)}
    owner = str(app.get("owner_login") or "").lower()
    is_owner = bool(owner) and owner == account["login"].lower()
    if public is None and not is_owner:
        public = app_is_public(app["slug"])
    install_url = github_urls.install_app_on(app["slug"], account.get("id"))
    app = {**app, "public": public, "install_url": install_url, "is_owner": is_owner,
           # A personal account's install page needs that very person's GitHub session (a wrong
           # one is a bare 404), so the console routes people through GitHub's own sign-in.
           "signin_install_url": github_urls.login_then(install_url)}
    # A private App installs only on its owner (BOSSAPP-1015/1016). Visibility GitHub would not
    # confirm (public is None) counts as private for a foreign personal target: a dead Install
    # button there is a raw 404, so the honest route is the direct connection. For an organisation
    # the page says what is wrong, so an unconfirmed App keeps the app route.
    foreign_person = not is_owner and account.get("type") != github_urls.ORGANIZATION
    path = "install" if is_owner or public is True or (public is None and not foreign_person) else "make_public"
    return {"path": path, "app": app, "installation": None, "line": _line(path, account, app)}


def lookup(db: Any, org_id: str, raw: str, *, person_id: str = "") -> dict[str, Any]:
    login = parse_account(raw)
    account = github_account(db, login)
    out = plan(db, org_id, account, by=person_id)
    return {"account": account, "connection": _connection(db, org_id, person_id, account["login"]), **out}


def watch(db: Any, org_id: str, login: str, account_id: str, *, person_id: str = "",
          account_type: Optional[str] = None) -> dict[str, Any]:
    """The sheet's poll while the person is on GitHub: no lookup of the account again (GitHub
    limits those), only the App's installations and, while it matters, its public page. The
    account's type comes back from the sheet (the lookup said it), so a personal target keeps the
    private-App rule between polls."""
    name = parse_account(login)
    if account_id and not str(account_id).isdigit():
        raise GitHubSetupError("bad_account_id", 422)
    account = {"login": name, "id": str(account_id or ""), "type": github_urls.account_type(account_type)}
    out = plan(db, org_id, account, by=person_id)
    return {"account": {"login": name, "id": account["id"]}, **out}


# ---- the installation's repositories, and allowing them --------------------------------------------------

def _known_installation(db: Any, org_id: str, installation_id: str) -> dict[str, Any]:
    inst = str(installation_id or "").strip()
    if not inst.isdigit():
        raise GitHubSetupError("bad_installation", 422)
    for row in github_setup.recorded_installations(db, org_id):
        if str(row["installation_id"]) == inst:
            return row
    cfg = github_setup.app_config(db, org_id)
    if str(cfg.get("installation_id") or "") == inst:
        return {"installation_id": inst, "account_login": cfg.get("account_login"),
                "account_type": cfg.get("account_type")}
    raise GitHubSetupError("unknown_installation", 404,
                           message="BOSS does not know that installation. Look the account up again.")


def _used_by(db: Any, org_id: str) -> dict[str, list[str]]:
    from sqlalchemy import text

    rows = db.execute(text(
        "SELECT DISTINCT lower(b.full_name) AS f, p.name FROM github_repository_bindings b "
        "JOIN projects p ON p.id = b.project_id JOIN workspaces w ON w.id = p.workspace_id "
        "WHERE w.org_id = :o AND b.status = 'active' ORDER BY p.name"), {"o": org_id}).mappings().all()
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(str(r["f"]), []).append(str(r["name"]))
    return out


def _registry_rows(db: Any, org_id: str) -> dict[str, dict[str, Any]]:
    from sqlalchemy import text

    def read() -> dict[str, dict[str, Any]]:
        rows = db.execute(text("SELECT * FROM github_repos WHERE org_id = :o"), {"o": org_id}).mappings().all()
        return {str(r["full_name"]).lower(): dict(r) for r in rows}
    return github_setup._savepoint(db, read, {})  # noqa: SLF001


def repositories(db: Any, org_id: str, installation_id: str) -> dict[str, Any]:
    from ..multirepo import registry

    row = _known_installation(db, org_id, installation_id)
    try:
        listed = github_setup.installation_repositories(db, str(row["installation_id"]))
    except GitHubSetupError as exc:
        raise GitHubSetupError("repositories_unavailable", 502,
                               message="GitHub did not list this account's repositories. Try again.") from exc
    used = _used_by(db, org_id)
    known = _registry_rows(db, org_id)
    repos = []
    for item in sorted(listed, key=lambda r: r["full_name"].lower()):
        reg = known.get(item["full_name"].lower())
        state = registry.state_of(reg) if reg else "not_allowed"
        repos.append({"full_name": item["full_name"], "private": item["private"],
                      "default_branch": item.get("default_branch"),
                      "used_by": used.get(item["full_name"].lower(), []),
                      "allowed": bool(reg and reg.get("allowed")), "state": state,
                      "words": WORDS.get(state, "") if reg and reg.get("allowed") else ""})
    return {"installation_id": str(row["installation_id"]), "account": row.get("account_login"),
            "account_type": row.get("account_type"), "repos": repos}


#: Each registry state, in words.
WORDS = {
    "working": "Working: BOSS can read it, push branches and open pull requests.",
    "read_only": "Read only: BOSS can read it but not push. Give the BOSS app Contents: Read and write on GitHub.",
    "needs_fix": "Needs a fix on GitHub.",
    "checking": "Being checked.",
    "not_allowed": "Not allowed for BOSS.",
    "archived": "Archived on GitHub, so BOSS cannot use it.",
    "unreachable": "The BOSS app no longer reaches it. Add it to the installation on GitHub.",
}


def words_for(row: dict[str, Any]) -> str:
    state = row.get("state") or ""
    if state != "needs_fix":
        return WORDS.get(state, state)
    checks = row.get("checks") or {}
    if (checks.get("read") or {}).get("ok") is False:
        return "GitHub refused reading it. Check the installation includes it."
    if (checks.get("write") or {}).get("ok") is False:
        return "BOSS can read it but GitHub refused a push. Give the app Contents: Read and write."
    if (checks.get("pull_requests") or {}).get("ok") is False:
        return "Pushes work, but GitHub refused opening a pull request. Give the app Pull requests: Read and write."
    return WORDS["needs_fix"]


def allow(db: Any, settings: Any, project_id: str, installation_id: str, names: list[str], *,
          actor: str, auth: Any = None) -> dict[str, Any]:
    """Allow the picked repositories in the org's registry (the registry's own allow, which runs
    the three checks). Only repositories this installation lists are accepted."""
    from ..multirepo import registry
    from ..multirepo.common import org_of_project

    org_id = org_of_project(db, project_id) or ""
    row = _known_installation(db, org_id, installation_id)
    listed = {r["full_name"].lower(): r["full_name"]
              for r in github_setup.installation_repositories(db, str(row["installation_id"]))}
    wanted = []
    for name in names:
        full = listed.get(str(name or "").strip().lower())
        if full is None:
            raise GitHubSetupError("not_in_installation", 422, repository=str(name)[:200],
                                   message=f"{str(name)[:200]} is not one of {row.get('account_login') or 'this account'}'s "
                                           "repositories the BOSS app reaches.")
        if full not in wanted:
            wanted.append(full)
    if not wanted:
        raise GitHubSetupError("nothing_picked", 422, message="Pick at least one repository.")
    registry.refresh(db, settings, project_id, actor=actor)
    results = []
    for full in wanted:
        reg = registry.by_full_name(db, org_id, full)
        if reg is None:
            results.append({"full_name": full, "state": "unreachable", "allowed": False,
                            "words": WORDS["unreachable"], "checks": {}})
            continue
        shaped = registry.allow(db, settings, project_id, reg["id"], True, actor=actor, auth=auth)
        results.append({"full_name": full, "id": shaped["id"], "state": shaped["state"],
                        "allowed": shaped["allowed"], "checks": shaped["checks"], "words": words_for(shaped)})
    working = sum(1 for r in results if r["state"] == "working")
    return {"results": results, "working": working,
            "line": (f"{working} of {len(results)} working." if results else "")}
