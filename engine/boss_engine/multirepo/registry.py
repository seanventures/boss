"""The org's repository registry (Settings › Repo and models › Repositories).

Every repository the GitHub App installation reaches (or, with no App, the org's token), one row
each in `github_repos`. The operator ticks the ones BOSS may use; a project then chooses only from
those. Ticking runs the three checks first — read, write, open a PR — against GitHub itself
(setup_checks.probe_repository, the same probes Setup uses): nothing says Working until all three
pass. A repository the App can only read is allowed as "Read only" (deploy config).

Unticking removes the repository from every project of the org (its bindings are disabled) and
from the next token mint (`mint_allowed`); a run already holding a token keeps it until it ends —
GitHub's one-hour expiry and the run-end revocation are the backstop.

An install that never used the registry (no rows for the org) keeps today's behaviour: nothing
here refuses anything until the registry has been refreshed once.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from ..services.core import make_id
from .common import MultiRepoError, event, iso, jdump, jload, norm_repo, org_of_project, sql

#: The pill a registry row shows, in the order the filters count them.
STATES = ("working", "read_only", "needs_fix", "checking", "not_allowed", "archived", "unreachable")

Lister = Callable[[Any, Any, str, str], tuple[str, list[dict[str, Any]]]]
Prober = Callable[[Any, Any, str, str, str], dict[str, Any]]


# ── where the list comes from ──────────────────────────────────────────────────────────────────

def _list_from_app(db: Any) -> list[dict[str, Any]]:
    from ..identity import github_app, github_setup

    token = github_setup.installation_token_all(db)
    out: list[dict[str, Any]] = []
    for page in range(1, 11):
        resp = github_app._call("GET", f"/installation/repositories?per_page=100&page={page}",  # noqa: SLF001
                                token=token)
        if resp.status != 200:
            raise MultiRepoError(502, "github_unreachable",
                                 "GitHub did not list the App's repositories. Try again.")
        items = (resp.json() or {}).get("repositories") or []
        out.extend(i for i in items if isinstance(i, dict))
        if len(items) < 100:
            break
    return out


def _list_from_org_token(db: Any, org_id: str) -> list[dict[str, Any]]:
    """Every repository the org's tokens were given for — all of them, one per GitHub account
    (BOSSAPP-808), not only the newest token's."""
    from ..identity import github_app, github_setup, vault

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tok in github_setup.org_tokens(db, org_id):
        secret = vault.org_github_token(tok["id"], db=db)
        for full in tok["repos"][:200]:
            if full.lower() in seen:
                continue
            seen.add(full.lower())
            resp = github_app._call("GET", f"/repos/{full}", token=secret)  # noqa: SLF001
            if resp.status == 200 and isinstance(resp.json(), dict):
                out.append(resp.json())
            else:
                out.append({"full_name": full})
    return out


def default_lister(db: Any, settings: Any, org_id: str, project_id: str) -> tuple[str, list[dict[str, Any]]]:
    from ..identity import github_setup

    if github_setup.app_ready(db, org_id):
        return "app", _list_from_app(db)
    if github_setup.org_token(db, org_id) is not None:
        return "token", _list_from_org_token(db, org_id)
    raise MultiRepoError(409, "github_not_connected",
                         "Connect GitHub first (Setup › GitHub access): the registry lists what the "
                         "GitHub App can reach.")


# ── refresh ────────────────────────────────────────────────────────────────────────────────────

def _registry_in_use(db: Any, org_id: str) -> bool:
    return db.execute(sql("SELECT 1 FROM github_repos WHERE org_id = :o LIMIT 1"), {"o": org_id}).first() is not None


def refresh(db: Any, settings: Any, project_id: str, *, actor: str = "operator",
            lister: Optional[Lister] = None) -> dict[str, Any]:
    """Re-read what GitHub lists and upsert it. New rows start NOT allowed — except the first
    refresh of an org, which allows every repository a project already uses (so turning the
    registry on never takes a working repository away from anyone)."""
    org_id = org_of_project(db, project_id)
    if not org_id:
        raise MultiRepoError(404, "unknown_project", "No such project.")
    first = not _registry_in_use(db, org_id)
    source, items = (lister or default_lister)(db, settings, org_id, project_id)
    seen: set[str] = set()
    for item in items:
        full = str(item.get("full_name") or "").strip()
        if "/" not in full:
            continue
        key = norm_repo(full)
        if key in seen:
            continue
        seen.add(key)
        db.execute(sql(
            "INSERT INTO github_repos (id, org_id, source, github_repo_id, full_name, description, "
            "default_branch, language, private, archived, reachable, last_seen_at) "
            "VALUES (:id, :o, :src, :gid, :full, :desc, :branch, :lang, :priv, :arch, true, now()) "
            "ON CONFLICT (org_id, lower(full_name)) DO UPDATE SET source = EXCLUDED.source, "
            "github_repo_id = COALESCE(EXCLUDED.github_repo_id, github_repos.github_repo_id), "
            "full_name = EXCLUDED.full_name, description = COALESCE(EXCLUDED.description, github_repos.description), "
            "default_branch = COALESCE(EXCLUDED.default_branch, github_repos.default_branch), "
            "language = COALESCE(EXCLUDED.language, github_repos.language), private = EXCLUDED.private, "
            "archived = EXCLUDED.archived, reachable = true, last_seen_at = now(), updated_at = now()"),
            {"id": make_id("ghr"), "o": org_id, "src": source,
             "gid": item.get("id") if isinstance(item.get("id"), int) else None, "full": full,
             "desc": (str(item["description"])[:300] if item.get("description") else None),
             "branch": item.get("default_branch"), "lang": item.get("language"),
             "priv": bool(item.get("private")) if item.get("private") is not None else None,
             "arch": bool(item.get("archived"))})
    # Gone from the installation: not reachable, never silently still allowed.
    rows = db.execute(sql("SELECT id, full_name FROM github_repos WHERE org_id = :o"), {"o": org_id}).mappings().all()
    gone = [r["id"] for r in rows if norm_repo(r["full_name"]) not in seen]
    if gone:
        db.execute(sql("UPDATE github_repos SET reachable = false, allowed = false, updated_at = now() "
                       "WHERE id = ANY(:ids)"), {"ids": gone})
    if first:
        used = _used_full_names(db, org_id)
        db.execute(sql("UPDATE github_repos SET allowed = true, allowed_by = :a, allowed_at = now() "
                       "WHERE org_id = :o AND reachable AND NOT archived AND lower(full_name) = ANY(:u)"),
                   {"o": org_id, "a": actor, "u": sorted(used)})
    event(db, project_id, "github.registry_refreshed", actor, "org", org_id,
          {"source": source, "listed": len(seen), "gone": len(gone)})
    return {"source": source, "listed": len(seen), "gone": len(gone), "first": first}


def _used_full_names(db: Any, org_id: str) -> set[str]:
    rows = db.execute(sql(
        "SELECT DISTINCT lower(b.full_name) FROM github_repository_bindings b JOIN projects p ON p.id = b.project_id "
        "JOIN workspaces w ON w.id = p.workspace_id WHERE w.org_id = :o AND b.status = 'active'"),
        {"o": org_id}).scalars().all()
    return {str(r) for r in rows}


# ── reading ────────────────────────────────────────────────────────────────────────────────────

def state_of(row: dict[str, Any]) -> str:
    if row.get("archived"):
        return "archived"
    if not row.get("reachable", True):
        return "unreachable"
    if not row.get("allowed"):
        return "not_allowed"
    checks = jload(row.get("checks"), {}) or {}
    if not checks or not row.get("checked_at"):
        return "checking"
    read = (checks.get("read") or {}).get("ok")
    write = (checks.get("write") or {}).get("ok")
    prs = (checks.get("pull_requests") or {}).get("ok")
    if read and write and prs:
        return "working"
    if read and not write:
        return "read_only"
    return "needs_fix"


def _shape(row: dict[str, Any], used_by: list[dict[str, Any]]) -> dict[str, Any]:
    checks = jload(row.get("checks"), {}) or {}
    return {
        "id": row["id"], "full_name": row["full_name"], "description": row.get("description"),
        "default_branch": row.get("default_branch") or "main", "language": row.get("language"),
        "private": row.get("private"), "archived": bool(row.get("archived")),
        "reachable": bool(row.get("reachable", True)), "allowed": bool(row.get("allowed")),
        "source": row.get("source"),
        "checks": {k: {"ok": (checks.get(k) or {}).get("ok"), "state": (checks.get(k) or {}).get("state")}
                   for k in ("read", "write", "pull_requests")},
        "checked_at": iso(row.get("checked_at")), "state": state_of(row),
        "used_by": used_by,
    }


_FILL_TRIED: dict[str, float] = {}
_FILL_EVERY_S = 600.0


def _may_fill(org_id: str) -> bool:
    """At most one GitHub read per org every ten minutes while it lists nothing."""
    import time

    now = time.monotonic()
    if now - _FILL_TRIED.get(org_id, -_FILL_EVERY_S) < _FILL_EVERY_S:
        return False
    _FILL_TRIED[org_id] = now
    return True


def list_registry(db: Any, project_id: str, *, settings: Any = None,
                  lister: Optional[Lister] = None) -> dict[str, Any]:
    """The org's registry. FIRST READ FILLS IT (boss-1, 2026-11-05): nothing had ever pressed
    "Refresh from GitHub", so Advanced › Repositories said "reaches 0 repositories · Save · 0
    allowed" beside two working repos, and Import a repo listed nothing to import. With `settings`
    (the API's read), an org whose registry was never filled is read from GitHub once — the first
    refresh allows every repository a project already uses, so nothing working is taken away. A
    GitHub that cannot be reached leaves the empty list, as before."""
    org_id = org_of_project(db, project_id)
    if not org_id:
        raise MultiRepoError(404, "unknown_project", "No such project.")
    if settings is not None and not _registry_in_use(db, org_id) and _may_fill(org_id):
        try:
            with db.begin_nested():
                refresh(db, settings, project_id, actor="system", lister=lister)
        except Exception:  # noqa: BLE001 — unreachable GitHub: show what is known (nothing)
            pass
    rows = [dict(r) for r in db.execute(sql(
        "SELECT * FROM github_repos WHERE org_id = :o ORDER BY allowed DESC, archived, lower(full_name)"),
        {"o": org_id}).mappings().all()]
    users = db.execute(sql(
        "SELECT lower(b.full_name) AS f, p.id AS project_id, p.name FROM github_repository_bindings b "
        "JOIN projects p ON p.id = b.project_id JOIN workspaces w ON w.id = p.workspace_id "
        "WHERE w.org_id = :o AND b.status = 'active' ORDER BY p.name"), {"o": org_id}).mappings().all()
    by_repo: dict[str, list[dict[str, Any]]] = {}
    for u in users:
        by_repo.setdefault(u["f"], []).append({"project_id": u["project_id"], "name": u["name"]})
    items = [_shape(r, by_repo.get(norm_repo(r["full_name"]), [])) for r in rows]
    counts = {"all": len(items), "allowed": sum(1 for i in items if i["allowed"]),
              "needs_fix": sum(1 for i in items if i["state"] in ("needs_fix", "read_only")),
              "not_allowed": sum(1 for i in items if not i["allowed"])}
    return {"org_id": org_id, "items": items, "counts": counts, "in_use": bool(items),
            "connection": _connection_summary(db, org_id)}


def _connection_summary(db: Any, org_id: str) -> dict[str, Any]:
    from ..identity import github_setup

    try:
        cfg = github_setup.app_config(db, org_id)
        if cfg.get("source") != "setup":
            cfg = github_setup.app_config(db)
        links = github_setup.links(cfg) if cfg.get("source") == "setup" else {}
    except Exception:  # noqa: BLE001 — no identity tables: no App to describe
        cfg, links = {}, {}
    return {"kind": "app" if cfg.get("source") == "setup" else "token",
            "account_login": cfg.get("account_login"), "account_type": cfg.get("account_type"),
            "app_slug": cfg.get("slug"),
            # Where the operator changes which repositories the App reaches (github_urls).
            "change_url": links.get("installation") or links.get("install")}


def get_row(db: Any, org_id: str, repo_id: str) -> dict[str, Any]:
    row = db.execute(sql("SELECT * FROM github_repos WHERE id = :i AND org_id = :o"),
                     {"i": repo_id, "o": org_id}).mappings().first()
    if row is None:
        raise MultiRepoError(404, "unknown_repository", "No such repository in this organization's registry.")
    return dict(row)


def by_full_name(db: Any, org_id: str, full_name: str) -> Optional[dict[str, Any]]:
    row = db.execute(sql("SELECT * FROM github_repos WHERE org_id = :o AND lower(full_name) = :f"),
                     {"o": org_id, "f": norm_repo(full_name)}).mappings().first()
    return dict(row) if row else None


# ── the checks ───────────────────────────────────────────────────────────────────────────────

def default_prober(db: Any, settings: Any, org_id: str, project_id: str, full_name: str) -> dict[str, Any]:
    """Probe with a credential limited to THIS repository: an installation token minted for it
    alone (write first; read-only when the App holds only read there), else the org token."""
    from ..identity import github_app, github_setup, vault
    from ..services import setup_checks

    owner, _, name = full_name.partition("/")
    token: Optional[str] = None
    try:
        token = github_setup.control_plane_token(db, owner, name)
    except Exception:  # noqa: BLE001 — write was refused: try to read
        token = None
    if token is None and github_setup.app_ready(db, org_id):
        try:
            tok = github_app.mint_installation_token([full_name], {"contents": "read", "metadata": "read"}, db=db)
            github_setup._account_for(tok)  # noqa: SLF001 — revoked like every control-plane token
            token = tok.token
        except Exception:  # noqa: BLE001
            token = None
    if token is None:
        org = github_setup.org_token_for(db, norm_repo(full_name), org_id)
        if org is not None:
            token = vault.org_github_token(org["id"], db=db)
    if not token:
        at = setup_checks.iso(setup_checks.now_utc())
        return {"state": "no_access", "checks": {"read": {"ok": False, "state": "no_access", "at": at},
                                                 "write": {"ok": None, "state": "not_run", "at": None},
                                                 "pull_requests": {"ok": None, "state": "not_run", "at": None}}}
    return setup_checks.probe_repository(token, full_name, api_base=github_app.api_base())


def check(db: Any, settings: Any, project_id: str, repo_id: str, *, actor: str = "operator",
          prober: Optional[Prober] = None) -> dict[str, Any]:
    org_id = org_of_project(db, project_id) or ""
    row = get_row(db, org_id, repo_id)
    found = (prober or default_prober)(db, settings, org_id, project_id, row["full_name"])
    checks = {**(found.get("checks") or {}), "state": found.get("state")}
    db.execute(sql("UPDATE github_repos SET checks = CAST(:c AS JSONB), checked_at = now(), "
                   "default_branch = COALESCE(:b, default_branch), updated_at = now() WHERE id = :i"),
               {"c": jdump(checks), "b": found.get("default_branch"), "i": repo_id})
    event(db, project_id, "github.registry_checked", actor, "github_repo", repo_id,
          {"repository": row["full_name"], "state": found.get("state")})
    return _shape(get_row(db, org_id, repo_id), [])


def set_allowed(db: Any, settings: Any, project_id: str, repo_id: str, allowed: bool, *,
                actor: str = "operator", prober: Optional[Prober] = None) -> dict[str, Any]:
    org_id = org_of_project(db, project_id) or ""
    row = get_row(db, org_id, repo_id)
    if allowed:
        if row.get("archived"):
            raise MultiRepoError(409, "archived", f"{row['full_name']} is archived on GitHub; it cannot be allowed.")
        if not row.get("reachable", True):
            raise MultiRepoError(409, "unreachable",
                                 f"The GitHub App no longer reaches {row['full_name']}. Add it in GitHub first.")
        db.execute(sql("UPDATE github_repos SET allowed = true, allowed_by = :a, allowed_at = now(), "
                       "updated_at = now() WHERE id = :i"), {"a": actor, "i": repo_id})
        shaped = check(db, settings, project_id, repo_id, actor=actor, prober=prober)
        event(db, project_id, "github.repository_allowed", actor, "github_repo", repo_id,
              {"repository": row["full_name"], "state": shaped["state"]})
        return shaped
    db.execute(sql("UPDATE github_repos SET allowed = false, allowed_by = :a, allowed_at = now(), "
                   "updated_at = now() WHERE id = :i"), {"a": actor, "i": repo_id})
    removed = _remove_everywhere(db, org_id, row["full_name"], actor)
    event(db, project_id, "github.repository_disallowed", actor, "github_repo", repo_id,
          {"repository": row["full_name"], "removed_from_projects": removed})
    return _shape(get_row(db, org_id, repo_id), [])


def allow(db: Any, settings: Any, project_id: str, repo_id: str, allowed: bool, *, actor: str = "operator",
          auth: Any = None, prober: Optional[Prober] = None) -> dict[str, Any]:
    """Tick / untick one repository for BOSS, with its security audit event. The console's PATCH
    /v1/github/repos/{id} and the agent's allow_repository both come here."""
    row = set_allowed(db, settings, project_id, repo_id, allowed, actor=actor, prober=prober)
    if auth is not None:
        from ..identity import audit

        audit.record(audit.SecurityEvent.from_context(
            auth, "repository.allowed" if allowed else "repository.disallowed", audit.Outcome.SUCCESS,
            target_type="github_repo", target_id=repo_id, permission="repository:register",
            scope=f"project:{project_id}", details={"repository": row["full_name"], "state": row["state"]}),
            db=db)
    return row


def _remove_everywhere(db: Any, org_id: str, full_name: str, actor: str) -> list[str]:
    """Unticked: out of every project (bindings disabled; a default moves to the next repo)."""
    from ..services import setup_actions

    rows = db.execute(sql(
        "SELECT b.id, b.project_id FROM github_repository_bindings b JOIN projects p ON p.id = b.project_id "
        "JOIN workspaces w ON w.id = p.workspace_id WHERE w.org_id = :o AND lower(b.full_name) = :f "
        "AND b.status = 'active'"), {"o": org_id, "f": norm_repo(full_name)}).mappings().all()
    for r in rows:
        setup_actions.remove_repository(db, r["project_id"], r["id"], actor=actor)
    return sorted({r["project_id"] for r in rows})


def save_allowed(db: Any, settings: Any, project_id: str, allowed_ids: list[str], *, actor: str = "operator",
                 prober: Optional[Prober] = None) -> dict[str, Any]:
    """The screen's "Save · N allowed": tick exactly these, untick the rest (one transaction)."""
    org_id = org_of_project(db, project_id) or ""
    want = set(allowed_ids)
    rows = db.execute(sql("SELECT id, allowed FROM github_repos WHERE org_id = :o"), {"o": org_id}).mappings().all()
    known = {r["id"] for r in rows}
    unknown = want - known
    if unknown:
        raise MultiRepoError(404, "unknown_repository", "Some repositories are not in the registry.",
                             ids=sorted(unknown))
    for r in rows:
        if r["id"] in want and not r["allowed"]:
            set_allowed(db, settings, project_id, r["id"], True, actor=actor, prober=prober)
        elif r["id"] not in want and r["allowed"]:
            set_allowed(db, settings, project_id, r["id"], False, actor=actor, prober=prober)
    return list_registry(db, project_id)


# ── enforcement ──────────────────────────────────────────────────────────────────────────────

def mint_allowed(db: Any, org_id: Optional[str], repos: list[str]) -> Optional[list[str]]:
    """The repositories of `repos` the registry REFUSES (unticked, archived, unreachable), or None
    when the registry is not in use for this org (an install that never refreshed it)."""
    if not org_id or not repos:
        return None
    try:
        with db.begin_nested():
            if not _registry_in_use(db, org_id):
                return None
            rows = db.execute(sql("SELECT lower(full_name) AS f, allowed, archived, reachable FROM github_repos "
                                  "WHERE org_id = :o AND lower(full_name) = ANY(:r)"),
                              {"o": org_id, "r": [norm_repo(r) for r in repos]}).mappings().all()
    except Exception:  # noqa: BLE001 — an older schema: the registry does not exist yet
        return None
    ok = {r["f"] for r in rows if r["allowed"] and not r["archived"] and r["reachable"]}
    return [r for r in repos if norm_repo(r) not in ok]
