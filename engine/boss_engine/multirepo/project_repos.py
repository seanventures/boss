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
    return {"project_id": project_id, "repo_mode": repo_mode(db, project_id), "items": items,
            "addable": addable, "not_allowed_count": not_allowed, "registry_in_use": bool(reg),
            "apps": _apps_summary(db, project_id)}


def _apps_summary(db: Any, project_id: str) -> list[dict[str, Any]]:
    from . import components

    try:
        return components.apps_with_components(db, project_id)
    except Exception:  # noqa: BLE001
        return []


def _connection_for(db: Any, settings: Any, project_id: str, actor: str) -> str:
    """The connection a new binding hangs off: the org's GitHub App (no token to paste), else the
    project's own working connection."""
    from ..identity import github_setup
    from ..services import setup_actions

    org_id = org_of_project(db, project_id)
    if org_id and github_setup.app_ready(db, org_id):
        return str(setup_actions.repository_from_app(db, settings, project_id, actor=actor,
                                                     list_repos=False)["connection_id"])
    row = db.execute(sql("SELECT id FROM github_connections WHERE project_id = :p AND status IN "
                         "('active', 'configured') ORDER BY (status = 'active') DESC, created_at LIMIT 1"),
                     {"p": project_id}).scalar()
    if row is None:
        raise MultiRepoError(409, "github_not_connected",
                             "Connect GitHub for this project first (Setup › GitHub access).")
    return str(row)


def add(db: Any, settings: Any, project_id: str, *, registry_repo_id: str, role_hint: Optional[str] = None,
        base_branch: Optional[str] = None, actor: str = "operator", check: bool = True) -> dict[str, Any]:
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
    connection_id = _connection_for(db, settings, project_id, actor)
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
