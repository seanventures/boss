"""A project's repositories (Factory › Projects › project › Repositories).

ADAPTED TO THE SCHEMA: SPEC.md names a `project_repos` table. A project's repositories already
are its `github_repository_bindings` (one row per repository, its connection, its checks, its
default = role 'primary'), and every build path resolves through them (repo_registry). So a project
repository IS an active binding; this module adds the multi-repo facts on it (`role_hint`, the
registry row it was chosen from) and the project's One repo / Several repos switch
(`repo_layouts`, app_key ''). Nothing is migrated: today's single binding is already the
project's one repository.

Rules:
  * a repository is added only from the registry's ALLOWED rows (when the registry is in use);
  * removing one is refused while an app's component uses it (the app would lose its code);
  * Several -> One is refused while any app has more than one component.
"""
from __future__ import annotations

from typing import Any, Optional

from .common import MultiRepoError, event, iso, jload, norm_repo, org_of_project, sql

ROLE_HINTS = ("contracts", "backend", "frontend", "deploy_config", "library", "service", "other")
ROLE_LABELS = {"contracts": "Contracts", "backend": "Backend", "frontend": "Frontend",
               "deploy_config": "Deploy config", "library": "Library", "service": "Service",
               "other": "Other"}


def repo_mode(db: Any, project_id: str, app_key: str = "") -> str:
    row = db.execute(sql("SELECT repo_mode FROM repo_layouts WHERE project_id = :p AND app_key = :a"),
                     {"p": project_id, "a": app_key or ""}).scalar()
    return str(row or "single")


def _set_layout(db: Any, project_id: str, app_key: str, mode: str, actor: str) -> None:
    db.execute(sql("INSERT INTO repo_layouts (project_id, app_key, repo_mode, updated_by, updated_at) "
                   "VALUES (:p, :a, :m, :by, now()) ON CONFLICT (project_id, app_key) DO UPDATE "
                   "SET repo_mode = EXCLUDED.repo_mode, updated_by = EXCLUDED.updated_by, updated_at = now()"),
               {"p": project_id, "a": app_key or "", "m": mode, "by": actor})


def _binding_rows(db: Any, project_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in db.execute(sql(
        "SELECT b.id, b.full_name, b.owner, b.name, b.role, b.role_hint, b.default_branch, b.checks, "
        "b.checked_at, b.registry_repo_id, b.connection_id, b.created_at "
        "FROM github_repository_bindings b WHERE b.project_id = :p AND b.status = 'active' "
        "ORDER BY (b.role = 'primary') DESC, b.created_at"), {"p": project_id}).mappings().all()]


def apps_using(db: Any, project_id: str) -> dict[str, list[dict[str, Any]]]:
    """binding id -> the apps whose components use it ({app_key, app_name, components})."""
    try:
        with db.begin_nested():
            rows = db.execute(sql(
                "SELECT binding_id, app_key, app_name, name FROM app_components WHERE project_id = :p "
                "ORDER BY app_name, position"), {"p": project_id}).mappings().all()
    except Exception:  # noqa: BLE001 — before components exist
        return {}
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for r in rows:
        apps = out.setdefault(r["binding_id"], {})
        app = apps.setdefault(r["app_key"], {"app_key": r["app_key"], "app_name": r["app_name"] or r["app_key"],
                                             "components": []})
        app["components"].append(r["name"])
    return {b: list(a.values()) for b, a in out.items()}


def _state(binding: dict[str, Any], registry_row: Optional[dict[str, Any]]) -> str:
    from . import registry

    if registry_row is not None:
        if not registry_row.get("allowed"):
            return "not_allowed"
        state = registry.state_of(registry_row)
        if state != "checking":
            return state
    checks = jload(binding.get("checks"), {}) or {}
    if not checks:
        return "checking"
    read = (checks.get("read") or {}).get("ok")
    write = (checks.get("write") or {}).get("ok")
    prs = (checks.get("pull_requests") or {}).get("ok")
    if read and write and prs:
        return "working"
    return "read_only" if read and not write else "needs_fix"


def list_project_repos(db: Any, project_id: str) -> dict[str, Any]:
    from . import registry

    org_id = org_of_project(db, project_id)
    if not org_id:
        raise MultiRepoError(404, "unknown_project", "No such project.")
    bindings = _binding_rows(db, project_id)
    using = apps_using(db, project_id)
    reg = {norm_repo(r["full_name"]): dict(r) for r in db.execute(
        sql("SELECT * FROM github_repos WHERE org_id = :o"), {"o": org_id}).mappings().all()}
    items = []
    in_project = set()
    for b in bindings:
        key = norm_repo(b["full_name"])
        in_project.add(key)
        rr = reg.get(key)
        items.append({
            "binding_id": b["id"], "full_name": b["full_name"],
            "registry_repo_id": (rr or {}).get("id") or b.get("registry_repo_id"),
            "language": (rr or {}).get("language"), "description": (rr or {}).get("description"),
            "role_hint": b.get("role_hint"), "is_default": b["role"] == "primary",
            "base_branch": b.get("default_branch") or (rr or {}).get("default_branch") or "main",
            "state": _state(b, rr), "used_by_apps": using.get(b["id"], []),
            "checked_at": iso(b.get("checked_at")),
        })
    addable = [{"id": r["id"], "full_name": r["full_name"], "language": r.get("language"),
                "default_branch": r.get("default_branch") or "main", "state": registry.state_of(r)}
               for k, r in sorted(reg.items()) if r.get("allowed") and k not in in_project
               and not r.get("archived") and r.get("reachable", True)]
    not_allowed = sum(1 for k, r in reg.items() if not r.get("allowed") and k not in in_project)
    mode = repo_mode(db, project_id)
    # ONE REPO MEANS EVERY APP BUILDS FROM IT. Apps built before components existed have no
    # component rows, so the default repository read "Used by apps: —" while HelpDesk was
    # built from it. In one-repo mode the default binding is used by every app the project has.
    if mode != "multi":
        built = _apps_built_here(db, project_id)
        for item in items:
            if item["is_default"] and not item["used_by_apps"]:
                item["used_by_apps"] = built
    # NO ALLOW-LIST IS NOT "NOTHING ALLOWED". A project on a GitHub token (no GitHub App) has no
    # registry; its repositories work through the token. The console said "No repository is
    # allowed for BOSS yet" beside a repository marked Working.
    return {"project_id": project_id, "repo_mode": mode, "items": items,
            "addable": addable, "not_allowed_count": not_allowed, "registry_in_use": bool(reg),
            "allow_list": "in_use" if reg else ("not_set_up" if items else "empty"),
            "apps": _apps_summary(db, project_id)}


def _apps_built_here(db: Any, project_id: str) -> list[dict[str, Any]]:
    """The project's apps as the Factory names them (one per app identity)."""
    try:
        from ..services.code_task_service import list_code_task_runs
        from ..services.factory_view import _app_key_of_name, app_name, group_into_apps

        with db.begin_nested():
            runs = list_code_task_runs(db, project_id, 200, exclude_internal=True)
        out, seen = [], set()
        for group in group_into_apps(runs):
            name = app_name(group[0])
            key = _app_key_of_name(name)
            if key in seen:
                continue
            seen.add(key)
            out.append({"app_key": key, "app_name": name, "components": []})
        return out
    except Exception:  # noqa: BLE001 — a helpful column, never a failed page
        return []


def _apps_summary(db: Any, project_id: str) -> list[dict[str, Any]]:
    from . import components

    try:
        return components.apps_with_components(db, project_id)
    except Exception:  # noqa: BLE001
        return []


def _connection_for(db: Any, settings: Any, project_id: str, actor: str, *,
                    connection_id: Optional[str] = None, owner: str = "",
                    repo: str = "") -> str:
    """The connection a new binding hangs off.

    An explicit `connection_id` wins outright — the caller's choice, surfacing GitHub's own
    refusal if it turns out to be wrong. Otherwise: the org's GitHub App (no token to paste),
    else whichever of the project's own connections can actually REACH `owner/repo` (probed) —
    a project holding several accounts must not hand a new repository to "the newest working
    connection" when that account cannot even see it."""
    from ..identity import github_setup
    from ..services import setup_actions

    if connection_id:
        row = db.execute(sql("SELECT id FROM github_connections WHERE project_id = :p AND id = :c"),
                         {"p": project_id, "c": connection_id}).scalar()
        if row is None:
            raise MultiRepoError(404, "unknown_connection", "That GitHub connection is not in this project.")
        return str(row)

    full_name = f"{owner}/{repo}".strip("/") if owner and repo else ""
    org_id = org_of_project(db, project_id)
    if org_id and github_setup.app_ready(db, org_id):
        app_reaches = True
        if full_name:
            try:
                reachable = {r["full_name"].lower() for r in github_setup.installation_repositories(db)}
                app_reaches = full_name.lower() in reachable
            except Exception:  # noqa: BLE001 — an App that can't list repos can't be trusted to reach this one
                app_reaches = False
        if app_reaches:
            return str(setup_actions.repository_from_app(db, settings, project_id, actor=actor,
                                                         list_repos=False)["connection_id"])
    rows = db.execute(sql("SELECT id FROM github_connections WHERE project_id = :p AND status IN "
                         "('active', 'configured') ORDER BY (status = 'active') DESC, created_at"),
                     {"p": project_id}).scalars().all()
    if not rows:
        raise MultiRepoError(409, "github_not_connected",
                             "Connect GitHub for this project first (Setup › GitHub access).")
    if full_name:
        from ..services import setup_checks
        from ..services.github_connections import GitHubConnectionService

        service = GitHubConnectionService(settings)
        for row in rows:
            cid = str(row)
            try:
                ref = service._credential_ref(db, project_id, cid)  # noqa: SLF001
                kind, _, key = str(ref).partition(":")
                record = {"ref_kind": kind, "ref_key": key}
                for item in service.secret_refs.list_secret_refs(db, project_id, status="active", limit=200):
                    if item.get("ref_kind") == kind and item.get("ref_key") == key:
                        record = item
                        break
                token = service._resolve_secret(record, db)  # noqa: SLF001
                if not token:
                    continue
                probed = setup_checks.probe_repository(
                    token, full_name,
                    api_base=str(getattr(settings, "github_api_base_url", "") or
                                 "https://api.github.com"))
                if (probed.get("checks") or {}).get("read", {}).get("ok"):
                    return cid
            except Exception:  # noqa: BLE001 — a connection that cannot be probed cannot reach it either
                continue
        raise MultiRepoError(409, "github_repo_unreachable",
                             "None of this project's GitHub connections can reach that repository.")
    return str(rows[0])


def add(db: Any, settings: Any, project_id: str, *, registry_repo_id: str, role_hint: Optional[str] = None,
        base_branch: Optional[str] = None, actor: str = "operator", check: bool = True,
        connection_id: Optional[str] = None) -> dict[str, Any]:
    from . import registry
    from ..services import setup_actions

    org_id = org_of_project(db, project_id) or ""
    row = registry.get_row(db, org_id, registry_repo_id)
    if not row.get("allowed") or row.get("archived") or not row.get("reachable", True):
        raise MultiRepoError(409, "not_allowed",
                             f"{row['full_name']} is not allowed in Repo and models. Tick it there first.")
    hint = _hint(role_hint)
    has_primary = db.execute(sql("SELECT 1 FROM github_repository_bindings WHERE project_id = :p AND "
                                 "role = 'primary' AND status = 'active'"), {"p": project_id}).first()
    owner, _, name = row["full_name"].partition("/")
    connection_id = _connection_for(db, settings, project_id, actor, connection_id=connection_id,
                                   owner=owner, repo=name)
    if check:
        binding_id = setup_actions.select_repository(db, settings, project_id, connection_id, row["full_name"],
                                                     make_default=not has_primary, actor=actor)
    else:
        binding_id = _bind_unchecked(db, settings, project_id, connection_id, row["full_name"],
                                     make_default=not has_primary, actor=actor)
    db.execute(sql("UPDATE github_repository_bindings SET role_hint = :h, registry_repo_id = :r, "
                   "default_branch = COALESCE(:b, default_branch, :rb), updated_at = now() WHERE id = :i"),
               {"h": hint, "r": row["id"], "b": base_branch, "rb": row.get("default_branch"), "i": binding_id})
    event(db, project_id, "project.repository_added", actor, "github_repository_binding", binding_id,
          {"repository": row["full_name"], "role_hint": hint})
    return {"binding_id": binding_id, "full_name": row["full_name"], "role_hint": hint}


def _bind_unchecked(db: Any, settings: Any, project_id: str, connection_id: str, full_name: str, *,
                    make_default: bool, actor: str) -> str:
    """select_repository without the network probe (the registry has already checked it)."""
    from ..services import setup_actions

    service = setup_actions._service(settings)  # noqa: SLF001
    existing = db.execute(sql("SELECT id FROM github_repository_bindings WHERE project_id = :p AND "
                              "lower(full_name) = :f"), {"p": project_id, "f": norm_repo(full_name)}).scalar()
    if existing:
        db.execute(sql("UPDATE github_repository_bindings SET status = 'active', connection_id = :c, "
                       "updated_at = now() WHERE id = :b"), {"c": connection_id, "b": existing})
        binding_id = str(existing)
    else:
        binding_id = service.create_repository_binding(
            db, project_id=project_id, connection_id=connection_id, repository=full_name, role="source",
            default_branch=None, metadata={"setup": "multirepo"}, actor=actor)["id"]
    if make_default:
        setup_actions._promote(db, project_id, binding_id, actor)  # noqa: SLF001
    return binding_id


def _hint(value: Optional[str]) -> Optional[str]:
    if value in (None, ""):
        return None
    hint = str(value).strip().lower().replace(" ", "_")
    if hint not in ROLE_HINTS:
        raise MultiRepoError(422, "bad_role_hint", f"Role must be one of: {', '.join(ROLE_LABELS.values())}.")
    return hint


def _binding(db: Any, project_id: str, binding_id: str) -> dict[str, Any]:
    row = db.execute(sql("SELECT * FROM github_repository_bindings WHERE project_id = :p AND id = :i "
                         "AND status = 'active'"), {"p": project_id, "i": binding_id}).mappings().first()
    if row is None:
        raise MultiRepoError(404, "unknown_repository", "That repository is not in this project.")
    return dict(row)


def update(db: Any, project_id: str, binding_id: str, *, role_hint: Any = ..., base_branch: Any = ...,
           actor: str = "operator") -> dict[str, Any]:
    _binding(db, project_id, binding_id)
    if role_hint is not ...:
        db.execute(sql("UPDATE github_repository_bindings SET role_hint = :h, updated_at = now() WHERE id = :i"),
                   {"h": _hint(role_hint), "i": binding_id})
    if base_branch is not ... and base_branch:
        branch = str(base_branch).strip()
        if not branch or len(branch) > 200 or branch.startswith("boss/") or ".." in branch:
            raise MultiRepoError(422, "bad_branch", "That is not a base branch BOSS can build on.")
        db.execute(sql("UPDATE github_repository_bindings SET default_branch = :b, updated_at = now() WHERE id = :i"),
                   {"b": branch, "i": binding_id})
    event(db, project_id, "project.repository_updated", actor, "github_repository_binding", binding_id, {})
    return {"binding_id": binding_id}


def remove(db: Any, project_id: str, binding_id: str, *, actor: str = "operator") -> None:
    from ..services import setup_actions

    b = _binding(db, project_id, binding_id)
    users = apps_using(db, project_id).get(binding_id) or []
    if users:
        names = ", ".join(u["app_name"] for u in users)
        raise MultiRepoError(409, "in_use", f"{b['full_name']} is used by {names}. Move those components "
                                            "to another repository first.", apps=users)
    setup_actions.remove_repository(db, project_id, binding_id, actor=actor)
    event(db, project_id, "project.repository_removed", actor, "github_repository_binding", binding_id,
          {"repository": b["full_name"]})


def set_mode(db: Any, project_id: str, mode: str, *, actor: str = "operator") -> dict[str, Any]:
    mode = str(mode or "").strip().lower()
    if mode not in ("single", "multi"):
        raise MultiRepoError(422, "bad_mode", "Choose One repo or Several repos.")
    if mode == "single":
        try:
            with db.begin_nested():
                wide = db.execute(sql("SELECT app_name, count(*) AS n FROM app_components WHERE project_id = :p "
                                      "GROUP BY app_key, app_name HAVING count(*) > 1"),
                                  {"p": project_id}).mappings().all()
        except Exception:  # noqa: BLE001
            wide = []
        if wide:
            names = ", ".join(str(w["app_name"]) for w in wide)
            raise MultiRepoError(409, "apps_use_several",
                                 f"{names} {'is' if len(wide) == 1 else 'are'} built from several repositories. "
                                 "Bring each app down to one component first.",
                                 apps=[str(w["app_name"]) for w in wide])
    _set_layout(db, project_id, "", mode, actor)
    event(db, project_id, "project.repo_mode_set", actor, "project", project_id, {"repo_mode": mode})
    return {"project_id": project_id, "repo_mode": mode}


# ── one entry point per act, for the console route AND the agent's tools ──────────────────────
#
# PUT/DELETE /v1/projects/{project_id}/repos/{repo_id} and the agent's register_repository /
# remove_project_repository call these, so there is one implementation of "add this registry
# repository to that project" — not a route and a second copy in a tool.

def registry_id_for(db: Any, project_id: str, ref: str) -> str:
    """The org registry row a reference names: its id (ghr_…) or its owner/name."""
    from . import registry

    ref = str(ref or "").strip()
    if not ref:
        raise MultiRepoError(422, "bad_request", "Name the repository: owner/name or its registry id.")
    org_id = org_of_project(db, project_id)
    if not org_id:
        raise MultiRepoError(404, "unknown_project", "No such project.")
    if "/" in ref:
        row = registry.by_full_name(db, org_id, ref)
        if row is None:
            raise MultiRepoError(404, "unknown_repository",
                                 f"{ref} is not in this organization's repository registry. Refresh "
                                 "the registry; if it is still missing, the GitHub App does not reach it.")
        return str(row["id"])
    return str(registry.get_row(db, org_id, ref)["id"])


def binding_for(db: Any, project_id: str, ref: str) -> Optional[str]:
    """The project's active binding a reference names: the binding id, the registry row's id, or
    the repository's owner/name."""
    row = db.execute(sql(
        "SELECT b.id FROM github_repository_bindings b WHERE b.project_id = :p AND b.status = 'active' AND "
        "(b.id = :r OR b.registry_repo_id = :r OR lower(b.full_name) = lower(:r) OR lower(b.full_name) = "
        "(SELECT lower(full_name) FROM github_repos WHERE id = :r)) LIMIT 1"),
        {"p": project_id, "r": str(ref or "").strip()}).scalar()
    return str(row) if row else None


def put(db: Any, settings: Any, project_id: str, ref: str, *, role_hint: Any = ...,
        base_branch: Optional[str] = None, actor: str = "operator",
        connection_id: Optional[str] = None) -> dict[str, Any]:
    """Add a registry repository to the project, or update the one it already has. Without an
    explicit `connection_id` the connection is the org's App, else whichever of the project's
    own connections can reach the repository."""
    binding_id = binding_for(db, project_id, ref)
    if binding_id is None:
        add(db, settings, project_id, registry_repo_id=registry_id_for(db, project_id, ref),
            role_hint=None if role_hint is ... else role_hint, base_branch=base_branch, actor=actor,
            connection_id=connection_id)
    else:
        update(db, project_id, binding_id, role_hint=role_hint, base_branch=base_branch or ..., actor=actor)
    return list_project_repos(db, project_id)


def delete(db: Any, project_id: str, ref: str, *, actor: str = "operator") -> dict[str, Any]:
    binding_id = binding_for(db, project_id, ref)
    if binding_id is None:
        raise MultiRepoError(404, "unknown_repository", "That repository is not in this project.")
    remove(db, project_id, binding_id, actor=actor)
    return list_project_repos(db, project_id)
