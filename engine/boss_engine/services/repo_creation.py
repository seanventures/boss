"""ONE CREDENTIAL RESOLVER FOR MAKING GITHUB REPOSITORIES (BOSSAPP-1160).

Add project › Start a new app › New GitHub repo said "BOSS has no GitHub credential that can
create repositories" on an install that held three: the GitHub App registered through Repo and
models (installed on @paramg), and two project connections with personal access tokens
(@paramg, @paramg-kaapppu). `local_publish.default_reach` read only the ENVIRONMENT-configured
App (`BOSS_GITHUB_APP_*`) and the install-wide `BOSS_GITHUB_TOKEN` — the two credentials a
wizard-connected install never has. And Setup called the App "Connected" although it
was never granted `administration`, the permission that creates repositories.

This module is the one answer to "which credential makes `owner/name`, and why":

  repo_creation_credential(db, settings, owner)   → the credential, or the exact reason
  creating_owners(db, settings, project_id, …)    → every owner BOSS knows, with its answer
  pat_abilities(token, …)                         → what a token can do, from GitHub's own headers
  FLOW_PERMISSIONS                                → what each GitHub flow needs

The order is fixed, and said back:
  1. the Identity-registered GitHub App's installation on `owner` — an ORGANISATION whose
     installation accepted `administration: write` (a GitHub App cannot create repositories in
     a personal account: GitHub has no endpoint for it);
  2. the environment-configured App (`BOSS_GITHUB_APP_*`), as before;
  3. the project connections' personal access tokens whose account IS `owner` (a token creates
     under its own user), or whose user is an active member of the organisation `owner`;
  4. the install-wide `BOSS_GITHUB_TOKEN`.

Every candidate that cannot create says the missing permission and the one fix in words
("Grant Administration: Read and write to the boss-localhost App", "Add a personal access token
with the repo scope for paramg"), so the console never says "no credential" when it means
"this one lacks that".

Never logs or returns a token in any dict: tokens travel only on `RepoCreationCredential.token`.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from boss_engine.identity import github_urls

from sqlalchemy import text

log = logging.getLogger(__name__)

#: What each GitHub flow needs, in GitHub's App-permission names (the connection's health records
#: these on `granted_permissions` / `missing_permissions` beside the repository-level ones).
FLOW_PERMISSIONS: dict[str, dict[str, str]] = {
    "build": {"contents": "write", "pull_requests": "write"},
    "create_repository": {"administration": "write"},
}
#: The installation token minted to create a repository: administration, and contents for the
#: first push that follows (publish). Never wider.
CREATE_PERMISSIONS = {"administration": "write", "contents": "write", "metadata": "read"}

_LEVELS = {"none": 0, "read": 1, "write": 2, "admin": 3}
_FINE_GRAINED = re.compile(r"^github_pat_")
_CLASSIC = re.compile(r"^gh[pousr]_")


def _level(value: object) -> int:
    return _LEVELS.get(str(value or "none").lower(), 0)


def _covers(granted: dict[str, str], needed: dict[str, str]) -> list[str]:
    """The permissions in `needed` that `granted` does not reach."""
    return sorted(name for name, want in needed.items() if _level(granted.get(name)) < _level(want))


# ── what a token can do, from GitHub's own answer ───────────────────────────────────────────

def scopes_to_permissions(scopes_header: Optional[str]) -> dict[str, str]:
    """A classic token's `X-OAuth-Scopes` header as flow permissions. `repo` is full control of
    the user's repositories (create, push, pull requests); `public_repo` the same for public ones."""
    scopes = {s.strip() for s in str(scopes_header or "").split(",") if s.strip()}
    out: dict[str, str] = {}
    if scopes & {"repo", "public_repo"}:
        out.update({"contents": "write", "pull_requests": "write", "administration": "write"})
    elif scopes & {"repo:status"}:
        out["contents"] = "read"
    if "delete_repo" in scopes:
        out["administration"] = "admin"
    return out


def pat_abilities(token: str, *, api_base: str = "", http: Any = None) -> dict[str, Any]:
    """What a personal access token can do, read from GitHub (GET /user), never from its name.

    {ok, login, shape: classic|fine_grained|unknown, granted: [...], missing: [...],
     can_create: True|False|None, words}. A classic token says its scopes in `X-OAuth-Scopes`;
    a fine-grained token has no such header and GitHub only tells at the attempt, so `can_create`
    is None (unverified) with the words saying what it would need. Never includes the token."""
    from .setup_checks import _base, _gh_headers, _send_check, default_http

    http = http or default_http
    api_base = _base(api_base)
    _send_check(token, api_base)
    who = http("GET", f"{api_base.rstrip('/')}/user", headers=_gh_headers(token))
    shape = "fine_grained" if _FINE_GRAINED.match(token or "") else "classic" if _CLASSIC.match(token or "") else "unknown"
    out: dict[str, Any] = {"ok": who.status == 200, "login": None, "shape": shape, "granted": [], "missing": [],
                           "can_create": None, "words": ""}
    if who.status != 200:
        out["words"] = "GitHub rejected the token." if who.status in (401, 403) else "GitHub did not answer."
        return out
    out["login"] = (who.body or {}).get("login") if isinstance(who.body, dict) else None
    header = None
    for key, value in (who.headers or {}).items():
        if str(key).lower() == "x-oauth-scopes":
            header = value
    needed = {**FLOW_PERMISSIONS["build"], **FLOW_PERMISSIONS["create_repository"]}
    if header is not None and str(header).strip():
        granted = scopes_to_permissions(header)
        out["granted"] = sorted(granted)
        out["missing"] = _covers(granted, needed)
        out["can_create"] = "administration" not in out["missing"]
        out["words"] = ("can create repositories under its own account (repo scope)" if out["can_create"]
                        else "cannot create repositories: the token lacks the repo scope (missing: administration)")
        return out
    if shape == "fine_grained" or header is not None:
        out["words"] = ("a fine-grained token: GitHub does not list its permissions, so BOSS learns at the "
                        "attempt whether it has Administration: Read and write (all repositories)")
        return out
    out["words"] = "GitHub did not say what the token may do"
    return out


# ── the candidates ──────────────────────────────────────────────────────────────────────────

@dataclass
class Candidate:
    """One credential that might create a repository in `owner`, and what BOSS knows about it."""
    kind: str                          # identity_app | env_app | connection | install
    label: str                         # "boss-localhost App", "paramg (access token, Acme Shop)", …
    token_kind: str                    # "app" | "user" (create_repository's token_kind)
    can_create: Optional[bool]         # None: unverified, GitHub decides at the attempt
    words: str = ""                    # why / why not, one sentence without a subject
    fix: str = ""                      # the exact fix when it cannot
    missing: list[str] = field(default_factory=list)
    account_login: str = ""
    account_type: str = ""             # "User" | "Organization" | ""
    connection_id: Optional[str] = None
    project_id: Optional[str] = None
    mint: Optional[Callable[[], Optional[str]]] = field(default=None, repr=False)

    def summary(self) -> dict[str, Any]:
        return {"kind": self.kind, "label": self.label, "token_kind": self.token_kind,
                "can_create": self.can_create, "words": self.words, "fix": self.fix,
                "missing": list(self.missing), "account_login": self.account_login or None,
                "connection_id": self.connection_id, "project_id": self.project_id}


@dataclass
class RepoCreationCredential:
    owner: str
    ok: bool
    token: Optional[str] = field(default=None, repr=False)
    token_kind: str = "app"
    kind: str = ""
    label: str = ""
    verified: bool = False             # True: GitHub confirmed the permission before the attempt
    reason: str = ""                   # "" | no_github_credential
    words: str = ""
    fixes: list[str] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Everything but the token."""
        return {"owner": self.owner, "ok": self.ok, "token_kind": self.token_kind, "kind": self.kind,
                "label": self.label, "verified": self.verified, "reason": self.reason, "words": self.words,
                "fixes": list(self.fixes), "candidates": list(self.candidates)}


def _identity_app_candidates(db: Any, owner: str) -> list[Candidate]:
    """The Setup-registered App's installation on `owner` (identity/github_setup)."""
    from ..identity import github_setup

    try:
        cfg = github_setup.app_config(db)
    except Exception:  # noqa: BLE001 — no identity tables on this schema
        return []
    if cfg.get("source") != "setup" or not cfg.get("app_id"):
        return []
    name = f"{cfg.get('slug') or 'BOSS'} App"
    links = github_setup.links(cfg)
    installs = {str(r.get("account_login") or "").lower(): dict(r) for r in github_setup.recorded_installations(db)}
    if cfg.get("installation_id") and cfg.get("account_login"):
        installs.setdefault(str(cfg["account_login"]).lower(), {
            "installation_id": cfg["installation_id"], "account_login": cfg["account_login"],
            "account_type": cfg.get("account_type")})
    row = installs.get(owner.lower())
    if row is None:
        where = ", ".join(sorted(r.get("account_login") or "" for r in installs.values() if r.get("account_login"))) or "no account"
        return [Candidate("identity_app", name, "app", False, account_login="",
                          words=f"the {name} is installed on {where}, not on {owner}",
                          fix=f"Install the {name} on {owner} (Setup › Add GitHub account)")]
    inst = str(row.get("installation_id") or "")
    kind = str(row.get("account_type") or "")
    if kind == "User":
        return [Candidate("identity_app", name, "app", False, account_login=owner, account_type=kind,
                          words="a GitHub App cannot create repositories in a personal account",
                          fix=f"Add a personal access token with the repo scope for {owner} (Setup)")]
    granted = github_setup.installation_permissions(db, inst)
    if kind != "Organization":
        # The account's kind is not recorded (an older row): GitHub decides at the attempt.
        return [Candidate("identity_app", name, "app", None, account_login=owner, account_type=kind,
                          words="whether this installation's account is an organisation is not recorded yet; "
                                "GitHub decides at the attempt (press Check again in Setup)",
                          mint=lambda: _mint_app(db, inst))]
    if granted is None:
        return [Candidate("identity_app", name, "app", None, account_login=owner, account_type=kind,
                          words="GitHub did not say what the installation was granted",
                          mint=lambda: _mint_app(db, inst))]
    missing = _covers(granted, FLOW_PERMISSIONS["create_repository"])
    if missing:
        fix = f"Grant Administration: Read and write to the {name}"
        if links.get("permissions"):
            fix += f" ({links['permissions']}), then accept it on the installation"
        return [Candidate("identity_app", name, "app", False, missing=missing, account_login=owner,
                          account_type=kind, words=f"the installation lacks {', '.join(missing)}: write", fix=fix)]
    return [Candidate("identity_app", name, "app", True, account_login=owner, account_type=kind,
                      words="the installation accepted Administration: Read and write",
                      mint=lambda: _mint_app(db, inst))]


def _mint_app(db: Any, installation_id: str) -> Optional[str]:
    from ..identity import github_setup

    try:
        return github_setup.installation_token_all(db, installation_id, permissions=CREATE_PERMISSIONS)
    except Exception:  # noqa: BLE001 — GitHub refused the mint: said by the caller's attempt
        log.debug("repo creation: the App's installation token was not minted", exc_info=True)
        return None


def _env_app_candidates(settings: Any, owner: str, owner_type: str = "") -> list[Candidate]:
    """The App configured in the environment (BOSS_GITHUB_APP_*): its one installation creates
    only in ITS account — an organisation named `owner`. A person's account never (GitHub has no
    endpoint); another account's never."""
    if owner_type == "User":
        return []
    try:
        from .github_app_adapter import GitHubAppAdapter

        app = GitHubAppAdapter(settings)
        if not app.configured:
            return []
    except Exception:  # noqa: BLE001
        return []
    label = "GitHub App (server settings)"
    account = app.installation_account()
    if account is not None:
        if account["login"].lower() != owner.lower():
            return [Candidate("env_app", label, "app", False, account_login=account["login"], account_type=account["type"],
                              words=f"its installation is on {account['login'] or 'another account'}, not on {owner}")]
        if account["type"] == "User":
            return [Candidate("env_app", label, "app", False, account_login=owner, account_type="User",
                              words="a GitHub App cannot create repositories in a personal account",
                              fix=f"Add a personal access token with the repo scope for {owner} (Setup)")]

    def mint() -> Optional[str]:
        try:
            return app.installation_token(permissions={"administration": "write", "contents": "write"}).token
        except Exception:  # noqa: BLE001 — not granted: the next candidate may do
            return None

    if account is None:
        return [Candidate("env_app", label, "app", None, account_login="",
                          words="GitHub did not say which account it is installed on; GitHub decides at the attempt",
                          mint=mint)]
    missing = _covers(account["permissions"], FLOW_PERMISSIONS["create_repository"])
    if missing:
        return [Candidate("env_app", label, "app", False, missing=missing, account_login=owner, account_type="Organization",
                          words=f"the installation lacks {', '.join(missing)}: write",
                          fix="Grant Administration: Read and write to the server's GitHub App, then accept it on the installation")]
    return [Candidate("env_app", label, "app", True, account_login=owner, account_type="Organization",
                      words="the installation accepted Administration: Read and write", mint=mint)]


def _connection_rows(db: Any, project_id: Optional[str],
                     actor: Optional[str] = None) -> list[dict[str, Any]]:
    """Personal-access-token connections, this project's first, then the rest of its org's.

    ANOTHER PROJECT'S TOKEN IS A PERSON'S (#179 review): it is used here only when its owner
    (`created_by`) is the person acting, or when the connection was explicitly shared with the
    organisation (metadata `shared_with_org`). Each row says whether the acting person may see
    whose it is (`visible`: a member of its project, or its owner); a row they may not is named
    "a token from another project", never by its project or login."""
    sql = ("SELECT c.id, c.project_id, c.connection_label, c.account_login, c.api_base_url, c.status, "
           "       c.granted_permissions, c.missing_permissions, c.last_test_status, p.name AS project_name, "
           "       c.created_by, COALESCE(c.metadata->>'shared_with_org', '') = 'true' AS shared, "
           "       EXISTS (SELECT 1 FROM project_members m WHERE m.project_id = c.project_id "
           "               AND m.member_id = :a AND m.status = 'active') AS actor_member "
           "  FROM github_connections c JOIN projects p ON p.id = c.project_id "
           " WHERE c.auth_method = 'personal_access_token' AND c.status IN ('configured', 'active') "
           "   AND c.account_login IS NOT NULL ")
    if project_id:
        sql += ("AND p.workspace_id IN (SELECT w.id FROM workspaces w WHERE w.org_id = "
                "(SELECT w2.org_id FROM projects p2 JOIN workspaces w2 ON w2.id = p2.workspace_id WHERE p2.id = :p)) "
                "ORDER BY (c.project_id = :p) DESC, c.last_tested_at DESC NULLS LAST, c.created_at")
    else:
        sql += "ORDER BY c.last_tested_at DESC NULLS LAST, c.created_at"
    try:
        rows = [dict(r) for r in db.execute(text(sql), {"p": project_id, "a": actor or ""}).mappings().all()]
    except Exception:  # noqa: BLE001 — an older schema
        return []
    out = []
    for r in rows:
        own = project_id is not None and str(r.get("project_id")) == str(project_id)
        mine = bool(actor) and str(r.get("created_by") or "") == str(actor)
        if not (own or mine or r.get("shared")):
            continue
        r["own"] = own
        r["visible"] = own or mine or bool(r.get("actor_member"))
        out.append(r)
    return out


def token_label(row: dict[str, Any]) -> str:
    """A token connection's name as the acting person may read it."""
    login = str(row.get("account_login") or "")
    if row.get("own"):
        return f"{login} (access token, this project)"
    if not row.get("visible"):
        return "a token from another project"
    return f"{login} (access token from project {row.get('project_name') or row.get('connection_label')})"


_ABILITIES: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
ABILITIES_TTL_S = 300.0


def _abilities_cached(token: str, api_base: str) -> dict[str, Any]:
    """pat_abilities, remembered for ABILITIES_TTL_S per token (by digest, never the token)."""
    import hashlib
    import time

    key = (hashlib.sha256(token.encode()).hexdigest()[:24], api_base)
    hit = _ABILITIES.get(key)
    if hit and time.monotonic() < hit[0]:
        return hit[1]
    got = pat_abilities(token, api_base=api_base)
    _ABILITIES[key] = (time.monotonic() + ABILITIES_TTL_S, got)
    return got


def _org_member(token: str, owner: str, api_base: str) -> bool:
    from .setup_checks import _base, _gh_headers, _send_check, default_http

    api_base = _base(api_base)
    _send_check(token, api_base)
    got = default_http("GET", f"{api_base.rstrip('/')}/user/memberships/orgs/{owner}", headers=_gh_headers(token))
    return got.status == 200 and isinstance(got.body, dict) and str(got.body.get("state") or "") == "active"


def _connection_candidates(db: Any, settings: Any, owner: str, project_id: Optional[str], *,
                           owner_type: str = "", probe: bool = True,
                           actor: Optional[str] = None, api_base: str = "") -> list[Candidate]:
    """Project connections whose token creates in `owner`: its own user, or an organisation it
    is an active member of (checked at GitHub, only for an organisation owner, and only when
    `probe`: the owner picker reads no secrets and asks GitHub nothing)."""
    from .github_connections import GitHubConnectionService

    service = GitHubConnectionService(settings)
    out: list[Candidate] = []
    # THE TARGET HOST (BOSSAPP-1286): a repository is created on one GitHub — the install's,
    # unless the caller names another. A connection of another host is never a candidate, even
    # when its account has the owner's login (logins are only unique per host).
    target = github_urls.api_base(api_base)
    for row in _connection_rows(db, project_id, actor):
        if github_urls.api_base(row.get("api_base_url")) != target:
            continue
        login = str(row.get("account_login") or "")
        same = login.lower() == owner.lower()
        if not same and owner_type != "Organization":
            # Another account's token creates only in an ORGANISATION it belongs to — and only
            # when BOSS knows `owner` is one (the App's installations say); never a guess.
            continue
        # WHOSE IT IS (BOSSAPP-1338), as far as the acting person may know (#179 review).
        label = token_label(row)
        granted = [str(p) for p in (row.get("granted_permissions") or [])]
        missing = [str(p) for p in (row.get("missing_permissions") or [])]
        api_base = github_urls.api_base(row.get("api_base_url") or getattr(settings, "github_api_base_url", ""))

        def mint(row=row) -> Optional[str]:
            try:
                return service.token_for_connection(db, project_id=str(row["project_id"]), connection_id=str(row["id"]))
            except Exception:  # noqa: BLE001 — an unreadable secret: the next candidate may do
                return None

        if "administration" in granted:
            can, words = True, "its token has the repo scope (can create repositories under its own account)"
        elif "administration" in missing:
            can, words = False, "its token lacks the repo scope (missing: administration)"
        elif row.get("last_test_status"):
            can, words = None, "a fine-grained token: GitHub tells at the attempt whether it may create repositories"
        elif probe and row.get("own"):
            # TEST IT NOW, it is one GET /user (BOSSAPP-1338) — only this project's own token,
            # and once per 5 minutes (#179 review): never every connection on every probe.
            token = mint()
            tested = _abilities_cached(token, api_base) if token else {"ok": False, "words": "its secret could not be read"}
            if not tested.get("ok"):
                can, words = False, f"its token did not pass a check ({tested.get('words') or 'GitHub rejected it'})"
            else:
                can = tested.get("can_create")
                words = str(tested.get("words") or "its token's scopes were checked")
        else:
            can, words = None, "its token's scopes are not checked yet: press Test all in Setup"
        if not same:
            # An organisation owner: only a member's token creates there, and the organisation may
            # forbid personal tokens — never 'verified', whatever the scopes say.
            if probe:
                token = mint()
                if not token or not _org_member(token, owner, api_base):
                    continue
                words += f"; {login} is a member of {owner}, if {owner} allows personal tokens"
            else:
                words += f"; whether {login} may create in {owner} is checked when you press Create"
            if can is not False:
                can = None
        seen = row.get("visible", True)
        if not seen:
            words = words.replace(login, "its owner") if login else words
        out.append(Candidate("connection", label, "user", can, words=words,
                             fix=((f"Replace the token for {login} with one holding the repo scope (Setup)"
                                   if row.get("visible", True) else
                                   "Replace that token with one holding the repo scope (in its project's Setup)")
                                  if can is False else ""),
                             missing=["administration"] if can is False else [],
                             account_login=login if row.get("visible", True) else "",
                             account_type="User", connection_id=str(row["id"]) if seen else None,
                             project_id=str(row["project_id"]) if seen else None,
                             mint=mint))
    return out


def _install_candidates(settings: Any) -> list[Candidate]:
    token = str(getattr(settings, "github_token", "") or "").strip()
    if not token:
        return []
    return [Candidate("install", "BOSS_GITHUB_TOKEN (server settings)", "user", None,
                      words="the server's own token; GitHub decides at the attempt", mint=lambda: token)]


def candidates(db: Any, settings: Any, owner: str, *, project_id: Optional[str] = None,
               actor: Optional[str] = None,
               owner_type: str = "", probe: bool = True) -> list[Candidate]:
    """Every credential BOSS holds that might create in `owner`, in the order they are tried.
    `probe=False` (the owner picker): nothing reads a secret or asks GitHub about memberships."""
    out: list[Candidate] = []
    for source in (lambda: _identity_app_candidates(db, owner), lambda: _env_app_candidates(settings, owner, owner_type),
                   lambda: _connection_candidates(db, settings, owner, project_id, owner_type=owner_type,
                                                  probe=probe, actor=actor),
                   lambda: _install_candidates(settings)):
        try:
            out.extend(source())
        except Exception:  # noqa: BLE001 — one source failing never hides the others
            log.debug("repo creation: a credential source failed", exc_info=True)
    return out


# ── the resolver ────────────────────────────────────────────────────────────────────────────

_NO_CREDENTIAL = ("BOSS has no GitHub credential that can create repositories in {owner}: connect a GitHub "
                  "App with Administration: Read and write (for an organisation), or add a personal access "
                  "token with the repo scope for {owner}, in Setup.")


def repo_creation_credential(db: Any, settings: Any, owner: str, *, project_id: Optional[str] = None,
                             actor: Optional[str] = None,
                             mint: bool = True, owner_type: str = "") -> RepoCreationCredential:
    """THE credential that creates `owner/<name>`, and why — or the precise reason none can.

    A candidate GitHub CONFIRMED (the installation's granted permissions, a classic token's
    scopes) goes first; one GitHub only answers at the attempt (a fine-grained token, the env App,
    the server token) is used when no confirmed one exists. `mint=False` decides without minting
    a token (the owner picker asks about many owners at once)."""
    owner = str(owner or "").strip()
    found = candidates(db, settings, owner, project_id=project_id, owner_type=owner_type, probe=mint,
                       actor=actor)
    summaries = [c.summary() for c in found]
    for want in (True, None):
        for picked in (c for c in found if c.can_create is want):
            token = None
            if mint and picked.mint is not None:
                token = picked.mint()
                if not token:
                    continue       # GitHub refused the mint: the next candidate
            return RepoCreationCredential(owner, True, token=token, token_kind=picked.token_kind, kind=picked.kind,
                                          label=picked.label, verified=want is True,
                                          words=f"Will create as {picked.label}: {picked.words}.", candidates=summaries)
    fixes = [c.fix for c in found if c.fix]
    if not found:
        words = _NO_CREDENTIAL.format(owner=owner)
    else:
        said = "; ".join(f"{c.label}: {c.words}" for c in found)
        words = (f"BOSS has no GitHub credential that can create repositories in {owner}. {said}. "
                 + " ".join(f"{f}." for f in fixes))
        if "Administration" not in words:
            words += " A GitHub App needs Administration: Read and write to create repositories."
    return RepoCreationCredential(owner, False, token=None, token_kind="app", reason="no_github_credential",
                                  words=words, fixes=fixes, candidates=summaries)


def known_owners(db: Any, project_id: Optional[str], actor: Optional[str] = None) -> list[dict[str, str]]:
    """The GitHub accounts BOSS holds a credential for: the App's installations and the token
    connections' logins. [{login, type}] with the login's case as GitHub gave it."""
    from ..identity import github_setup

    out: dict[str, dict[str, str]] = {}

    def add(login: Any, kind: Any) -> None:
        name = str(login or "").strip()
        if name and name.lower() not in out:
            out[name.lower()] = {"login": name, "type": str(kind or "")}

    try:
        cfg = github_setup.app_config(db)
        if cfg.get("source") == "setup":
            add(cfg.get("account_login"), cfg.get("account_type"))
            for row in github_setup.recorded_installations(db):
                add(row.get("account_login"), row.get("account_type"))
    except Exception:  # noqa: BLE001
        pass
    for row in _connection_rows(db, project_id, actor):
        if row.get("visible"):
            add(row.get("account_login"), "User")
    return list(out.values())


def creating_owners(db: Any, settings: Any, project_id: str, owners: Optional[list[str]] = None,
                    actor: Optional[str] = None) -> list[dict[str, Any]]:
    """Every owner the new-repo picker may list, each with its creating credential or its fixes:
    [{login, type, can_create, verified, via, via_kind, words, fixes}]. `owners`: logins the
    caller already shows (the registry's), kept in the answer even when BOSS has nothing for them."""
    known = known_owners(db, project_id, actor)
    types = {k["login"].lower(): k["type"] for k in known}
    logins: list[str] = [k["login"] for k in known]
    for extra in owners or []:
        name = str(extra or "").strip()
        if name and name.lower() not in {known.lower() for known in logins}:
            logins.append(name)
    out: list[dict[str, Any]] = []
    for login in logins:
        got = repo_creation_credential(db, settings, login, project_id=project_id, mint=False, actor=actor,
                                       owner_type=types.get(login.lower(), ""))
        out.append({"login": login, "type": types.get(login.lower()) or None, "can_create": got.ok,
                    "verified": got.verified, "via": got.label or None, "via_kind": got.kind or None,
                    "words": got.words, "fixes": got.fixes})
    return out


__all__ = ["CREATE_PERMISSIONS", "Candidate", "FLOW_PERMISSIONS", "RepoCreationCredential", "candidates",
           "creating_owners", "known_owners", "pat_abilities", "repo_creation_credential", "scopes_to_permissions"]
