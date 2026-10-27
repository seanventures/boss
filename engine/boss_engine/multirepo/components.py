"""An app's components (Factory › Apps › app › Components; New project › Where the code lives).

A component is a part of an app that lives in one repository of its project (and a folder of it,
for monorepos): `contracts` in acme/contracts, `backend` in acme/api, `frontend` in acme/web. Its
`depends_on` names the components it builds against; the build and the deploy go in the
topological order of that graph, in waves (independent components together).

ADAPTED TO THE SCHEMA: the app has no table of its own (an app is the runs filed under one
`app_key`, agent_assignment.app_key), so components are keyed (project_id, app_key) and the app's
One repo / Several repos is `repo_layouts` (SPEC.md's `apps.repo_mode`). `depends_on` holds
component NAMES (unique in the app) rather than ids, so a whole-list PUT can name dependencies of
components it is creating in the same call. `repo_id` is the project's binding id.

Validation on save (all refusals are sentences):
  * every repository is one of the project's, and allowed in the registry;
  * names unique and present; dependencies name components of the same app; no cycles;
  * a deploy-config component is read only; a code component needs a repository BOSS can write;
  * at least one code component; a path is "/" or a clean relative folder.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from ..services.core import make_id
from .common import MultiRepoError, event, jdump, jload, norm_repo, org_of_project, sql

KINDS = ("code", "deploy_config")
ACCESS = ("write", "read")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,47}$")
_PATH = re.compile(r"^/([A-Za-z0-9_.\-]+(/[A-Za-z0-9_.\-]+)*)?/?$")


def app_key_of(name: str) -> str:
    from ..services.agent_assignment import app_key

    return app_key(name)


# ── reading ────────────────────────────────────────────────────────────────────────────────────

def _rows(db: Any, project_id: str, app_key: str) -> list[dict[str, Any]]:
    return [dict(r) for r in db.execute(sql(
        "SELECT c.*, b.full_name, b.default_branch, b.role_hint, b.checks, b.status AS binding_status "
        "FROM app_components c JOIN github_repository_bindings b ON b.id = c.binding_id "
        "WHERE c.project_id = :p AND c.app_key = :a ORDER BY c.position, c.name"),
        {"p": project_id, "a": app_key}).mappings().all()]


def waves(components: list[dict[str, Any]]) -> list[list[str]]:
    """Kahn's algorithm in layers: [[contracts], [backend], [frontend]]. Raises on a cycle."""
    names = [c["name"] for c in components]
    deps = {c["name"]: [d for d in (c.get("depends_on") or []) if d in names] for c in components}
    done: set[str] = set()
    out: list[list[str]] = []
    while len(done) < len(names):
        layer = [n for n in names if n not in done and all(d in done for d in deps[n])]
        if not layer:
            stuck = sorted(n for n in names if n not in done)
            raise MultiRepoError(422, "cycle", "Those dependencies go round in a circle: "
                                               + " → ".join(stuck) + ". Remove one.", components=stuck)
        out.append(layer)
        done.update(layer)
    return out


def order(components: list[dict[str, Any]]) -> list[str]:
    return [n for layer in waves(components) for n in layer]


def _shape(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "name": row["name"], "binding_id": row["binding_id"],
            "repository": row.get("full_name"), "path": row.get("path") or "/",
            "access": row.get("access") or "write", "kind": row.get("kind") or "code",
            "depends_on": list(row.get("depends_on") or []),
            "ship_target": jload(row.get("ship_target"), {}) or {},
            # The binding's branch, else None: a lane's build resolves it (build_base_branch).
            "base_branch": row.get("default_branch") or None,
            "repository_active": row.get("binding_status") == "active"}


def get(db: Any, project_id: str, app_key: str) -> dict[str, Any]:
    from .project_repos import repo_mode

    rows = _rows(db, project_id, app_key)
    shaped = [_shape(r) for r in rows]
    code = [c for c in shaped if c["kind"] == "code"]
    try:
        seq = order(code)
    except MultiRepoError:
        seq = [c["name"] for c in code]
    pos = {n: i for i, n in enumerate(seq)}
    code.sort(key=lambda c: pos.get(c["name"], 99))
    for i, c in enumerate(code):
        c["order"] = i + 1
    return {"project_id": project_id, "app_key": app_key,
            "app_name": (rows[0].get("app_name") if rows else None) or app_key,
            "repo_mode": repo_mode(db, project_id, app_key) if rows else "single",
            "components": code, "deploy_config": [c for c in shaped if c["kind"] == "deploy_config"],
            "waves": _safe_waves(code)}


def _safe_waves(code: list[dict[str, Any]]) -> list[list[str]]:
    try:
        return waves(code)
    except MultiRepoError:
        return [[c["name"] for c in code]]


def is_multi(db: Any, project_id: str, app_key: str) -> bool:
    """A multi-repo app: Several repos, and more than one code component (a one-component app is
    the single-repo path, unchanged)."""
    try:
        with db.begin_nested():
            n = db.execute(sql("SELECT count(*) FROM app_components WHERE project_id = :p AND app_key = :a "
                               "AND kind = 'code'"), {"p": project_id, "a": app_key}).scalar() or 0
    except Exception:  # noqa: BLE001 — before the table exists
        return False
    return n > 1


def app_repos(db: Any, project_id: str, app_key: str) -> list[dict[str, Any]]:
    """The app's repositories with the access the app needs in each (write wins in a monorepo)."""
    out: dict[str, dict[str, Any]] = {}
    for r in _rows(db, project_id, app_key):
        key = norm_repo(r["full_name"])
        cur = out.get(key)
        access = "write" if (r["kind"] == "code" and r["access"] == "write") else "read"
        if cur is None:
            out[key] = {"full_name": r["full_name"], "binding_id": r["binding_id"], "access": access,
                        "components": [r["name"]]}
        else:
            cur["components"].append(r["name"])
            if access == "write":
                cur["access"] = "write"
    return sorted(out.values(), key=lambda x: (x["access"] != "write", x["full_name"].lower()))


def apps_with_components(db: Any, project_id: str) -> list[dict[str, Any]]:
    rows = db.execute(sql("SELECT DISTINCT app_key FROM app_components WHERE project_id = :p ORDER BY app_key"),
                      {"p": project_id}).scalars().all()
    out = []
    for key in rows:
        app = get(db, project_id, str(key))
        chain = " → ".join((c["repository"] or "").split("/")[-1] for c in app["components"])
        cfg = ", ".join((c["repository"] or "").split("/")[-1] for c in app["deploy_config"])
        out.append({"app_key": app["app_key"], "app_name": app["app_name"], "chain": chain,
                    "deploys_with": cfg or None, "components": len(app["components"])})
    return out


# ── saving ─────────────────────────────────────────────────────────────────────────────────────

def _clean_path(value: Any) -> str:
    path = str(value or "/").strip() or "/"
    if not path.startswith("/"):
        path = "/" + path
    if ".." in path or not _PATH.match(path):
        raise MultiRepoError(422, "bad_path", f"“{path}” is not a folder BOSS can build in. Use / or a folder like /apps/web.")
    return path.rstrip("/") or "/"


def validate(db: Any, project_id: str, components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The whole list, cleaned, or a MultiRepoError naming the first problem."""
    from . import registry

    if not isinstance(components, list) or not components:
        raise MultiRepoError(422, "no_components", "An app needs at least one component.")
    if len(components) > 12:
        raise MultiRepoError(422, "too_many", "An app can have at most 12 components.")
    org_id = org_of_project(db, project_id) or ""
    bindings = {r["id"]: dict(r) for r in db.execute(sql(
        "SELECT id, full_name, checks, role_hint FROM github_repository_bindings WHERE project_id = :p "
        "AND status = 'active'"), {"p": project_id}).mappings().all()}
    by_name = {norm_repo(b["full_name"]): bid for bid, b in bindings.items()}
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_paths: set[tuple[str, str]] = set()
    for raw in components:
        name = str((raw or {}).get("name") or "").strip()
        if not _NAME.match(name):
            raise MultiRepoError(422, "bad_name", "Give each component a short name (letters, digits, spaces, - _ .).")
        if name.lower() in seen:
            raise MultiRepoError(422, "duplicate_name", f"Two components are called “{name}”.")
        seen.add(name.lower())
        bid = str(raw.get("binding_id") or raw.get("repo_id") or "")
        if not bid and raw.get("repository"):
            bid = by_name.get(norm_repo(str(raw["repository"])), "")
        if bid not in bindings:
            raise MultiRepoError(422, "repository_not_in_project",
                                 f"{name}: choose one of this project's repositories (Factory › Projects › Repositories).")
        full = bindings[bid]["full_name"]
        refused = registry.mint_allowed(db, org_id, [full])
        if refused:
            raise MultiRepoError(409, "not_allowed", f"{full} is not allowed in Repo and models.")
        kind = str(raw.get("kind") or "code")
        if kind not in KINDS:
            raise MultiRepoError(422, "bad_kind", f"{name}: a component is code or deploy config.")
        access = str(raw.get("access") or ("read" if kind == "deploy_config" else "write"))
        if access not in ACCESS:
            raise MultiRepoError(422, "bad_access", f"{name}: access is write or read.")
        if kind == "deploy_config" and access != "read":
            raise MultiRepoError(422, "deploy_config_read_only",
                                 f"{name}: deploy config is read only — BOSS plans deploys from it, it never changes it.")
        if kind == "code" and access != "write":
            raise MultiRepoError(422, "read_only_build_target",
                                 f"{name}: a read-only repository cannot be built. Make it deploy config, or give BOSS write.")
        if kind == "code":
            checks = jload(bindings[bid].get("checks"), {}) or {}
            if (checks.get("write") or {}).get("ok") is False:
                raise MultiRepoError(422, "read_only_build_target",
                                     f"{name}: BOSS can only read {full}, so it cannot be built there. "
                                     "Grant write in GitHub, or make it deploy config.")
        path = _clean_path(raw.get("path"))
        if (bid, path) in seen_paths and kind == "code":
            raise MultiRepoError(422, "same_folder", f"{name}: two components cannot share {full}{path}.")
        seen_paths.add((bid, path))
        deps = [str(d).strip() for d in (raw.get("depends_on") or []) if str(d).strip()]
        ship = raw.get("ship_target") if isinstance(raw.get("ship_target"), dict) else {}
        cleaned.append({"name": name, "binding_id": bid, "path": path, "access": access, "kind": kind,
                        "depends_on": deps, "ship_target": _clean_ship(ship)})
    code_names = {c["name"] for c in cleaned if c["kind"] == "code"}
    if not code_names:
        raise MultiRepoError(422, "no_code", "An app needs at least one component BOSS builds.")
    for c in cleaned:
        for d in c["depends_on"]:
            if d == c["name"]:
                raise MultiRepoError(422, "self_dependency", f"{c['name']} cannot depend on itself.")
            if d not in code_names:
                raise MultiRepoError(422, "unknown_dependency", f"{c['name']} needs “{d}”, which is not a component BOSS builds.")
        if c["kind"] == "deploy_config" and c["depends_on"]:
            c["depends_on"] = []
    waves([c for c in cleaned if c["kind"] == "code"])          # refuses a cycle
    return cleaned


def _clean_ship(ship: dict[str, Any]) -> dict[str, Any]:
    kind = str(ship.get("kind") or "host")
    if kind not in ("host", "package", "none"):
        raise MultiRepoError(422, "bad_ship_target", "Ships to a machine, as a package, or not at all.")
    out: dict[str, Any] = {"kind": kind}
    for key in ("target_id", "label", "package", "url"):
        if ship.get(key):
            out[key] = str(ship[key])[:200]
    return out


def put(db: Any, project_id: str, app_key: str, components: list[dict[str, Any]], *, app_name: Optional[str] = None,
        actor: str = "operator") -> dict[str, Any]:
    """Replace the app's whole component list (validated), and set its One / Several mode."""
    from .project_repos import _set_layout

    key = str(app_key or "").strip()
    if not key:
        raise MultiRepoError(422, "no_app", "Name the app.")
    cleaned = validate(db, project_id, components)
    seq = order([c for c in cleaned if c["kind"] == "code"])
    pos = {n: i for i, n in enumerate(seq)}
    running = _running_change_sets(db, project_id, key)
    if running:
        current = {(c["name"], c["binding_id"]) for c in _rows(db, project_id, key)}
        new = {(c["name"], c["binding_id"]) for c in cleaned}
        if current != new:
            raise MultiRepoError(409, "change_running",
                                 "A change is being built on this app. Change its repositories when it has finished.",
                                 change_sets=running)
    existing = {r["name"]: r["id"] for r in db.execute(sql(
        "SELECT id, name FROM app_components WHERE project_id = :p AND app_key = :a"),
        {"p": project_id, "a": key}).mappings().all()}
    keep = []
    for c in cleaned:
        cid = existing.get(c["name"]) or make_id("acmp")
        keep.append(cid)
        db.execute(sql(
            "INSERT INTO app_components (id, project_id, app_key, app_name, name, binding_id, path, access, kind, "
            "depends_on, ship_target, position, created_by) VALUES (:id, :p, :a, :an, :n, :b, :path, :acc, :k, "
            ":deps, CAST(:ship AS JSONB), :pos, :by) ON CONFLICT (project_id, app_key, name) DO UPDATE SET "
            "app_name = EXCLUDED.app_name, binding_id = EXCLUDED.binding_id, path = EXCLUDED.path, "
            "access = EXCLUDED.access, kind = EXCLUDED.kind, depends_on = EXCLUDED.depends_on, "
            "ship_target = EXCLUDED.ship_target, position = EXCLUDED.position, updated_at = now()"),
            {"id": cid, "p": project_id, "a": key, "an": app_name, "n": c["name"], "b": c["binding_id"],
             "path": c["path"], "acc": c["access"], "k": c["kind"], "deps": c["depends_on"],
             "ship": jdump(c["ship_target"]), "pos": pos.get(c["name"], 50), "by": actor})
    db.execute(sql("DELETE FROM app_components WHERE project_id = :p AND app_key = :a AND NOT (id = ANY(:keep))"),
               {"p": project_id, "a": key, "keep": keep})
    if app_name:
        db.execute(sql("UPDATE app_components SET app_name = :n WHERE project_id = :p AND app_key = :a"),
                   {"n": app_name, "p": project_id, "a": key})
    code = [c for c in cleaned if c["kind"] == "code"]
    _set_layout(db, project_id, key, "multi" if len(code) > 1 else "single", actor)
    event(db, project_id, "app.components_set", actor, "app", key,
          {"components": [{"name": c["name"], "binding_id": c["binding_id"], "access": c["access"]} for c in cleaned]})
    return get(db, project_id, key)


def _running_change_sets(db: Any, project_id: str, app_key: str) -> list[str]:
    try:
        with db.begin_nested():
            return [str(r) for r in db.execute(sql(
                "SELECT id FROM repo_change_sets WHERE project_id = :p AND app_key = :a AND state IN "
                "('planned', 'building', 'blocked', 'in_review', 'approved', 'merging', 'deploying')"),
                {"p": project_id, "a": app_key}).scalars().all()]
    except Exception:  # noqa: BLE001 — before change sets exist
        return []


# ── suggestions ──────────────────────────────────────────────────────────────────────────────

_ROLE_WORDS = (
    ("contracts", ("contract", "proto", "schema", "openapi", "spec", "idl", "api-spec", "types")),
    ("deploy_config", ("infra", "deploy", "terraform", "helm", "k8s", "kube", "ops", "platform", "iac", "charts")),
    ("frontend", ("web", "ui", "frontend", "front", "site", "client", "app", "console", "dashboard", "storefront")),
    ("backend", ("api", "server", "backend", "service", "svc", "core", "worker", "gateway")),
)
_ROLE_LANG = {"protobuf": "contracts", "thrift": "contracts", "hcl": "deploy_config", "dockerfile": "deploy_config",
              "go": "backend", "java": "backend", "kotlin": "backend", "python": "backend", "rust": "backend",
              "ruby": "backend", "c#": "backend", "typescript": "frontend", "javascript": "frontend",
              "vue": "frontend", "svelte": "frontend", "html": "frontend", "css": "frontend"}
_ROLE_NAME = {"contracts": "Contracts", "backend": "Backend", "frontend": "Frontend", "deploy_config": "Deploy config",
              "library": "Library", "service": "Service", "other": "Component"}
_ROLE_RANK = {"contracts": 0, "library": 1, "backend": 2, "service": 2, "frontend": 3, "other": 4, "deploy_config": 9}


def _guess(full_name: str, language: Optional[str], hint: Optional[str]) -> tuple[str, list[str]]:
    name = full_name.split("/")[-1].lower()
    reasons: list[str] = []
    if hint:
        return hint, [f"project role “{_ROLE_NAME.get(hint, hint)}”"] + ([language] if language else [])
    for role, words in _ROLE_WORDS:
        hit = next((w for w in words if re.search(rf"(^|[-_.]){re.escape(w)}", name)), None)
        if hit:
            reasons.append(f"name has “{hit}”")
            if language:
                reasons.append(language)
            return role, reasons
    lang_role = _ROLE_LANG.get(str(language or "").lower())
    if lang_role:
        return lang_role, [f"{language} repository"]
    return "other", ["no strong signal; check it"]


def suggest(db: Any, project_id: str, binding_ids: Iterable[str]) -> dict[str, Any]:
    """Components for these repositories, with the reasons, in build order (POST
    /v1/projects/{id}/suggest-components). Names and languages only; nothing is read from the code."""
    org_id = org_of_project(db, project_id) or ""
    ids = [str(b) for b in binding_ids]
    rows = {r["id"]: dict(r) for r in db.execute(sql(
        "SELECT b.id, b.full_name, b.role_hint, g.language FROM github_repository_bindings b "
        "LEFT JOIN github_repos g ON g.org_id = :o AND lower(g.full_name) = lower(b.full_name) "
        "WHERE b.project_id = :p AND b.status = 'active' AND b.id = ANY(:ids)"),
        {"o": org_id, "p": project_id, "ids": ids}).mappings().all()}
    missing = [i for i in ids if i not in rows]
    if missing:
        raise MultiRepoError(422, "repository_not_in_project", "Choose this project's repositories.", ids=missing)
    guesses = []
    for bid in ids:
        r = rows[bid]
        role, reasons = _guess(r["full_name"], r.get("language"), r.get("role_hint"))
        guesses.append({"binding_id": bid, "repository": r["full_name"], "language": r.get("language"),
                        "role": role, "reasons": reasons})
    guesses.sort(key=lambda g: (_ROLE_RANK.get(g["role"], 5), g["repository"].lower()))
    used: dict[str, int] = {}
    out = []
    code_before: list[str] = []
    for g in guesses:
        base = _ROLE_NAME.get(g["role"], "Component")
        used[base] = used.get(base, 0) + 1
        name = base if used[base] == 1 else f"{base} {used[base]}"
        deploy = g["role"] == "deploy_config"
        comp = {"name": name, "binding_id": g["binding_id"], "repository": g["repository"],
                "language": g["language"], "path": "/", "kind": "deploy_config" if deploy else "code",
                "access": "read" if deploy else "write",
                "depends_on": [] if deploy else _deps_for(g["role"], code_before),
                "reason": "Suggested: " + " · ".join(g["reasons"])}
        out.append(comp)
        if not deploy:
            code_before.append(name)
    return {"components": out}


def _deps_for(role: str, before: list[str]) -> list[str]:
    """Contracts first; everything after builds against what came before it."""
    return list(before) if role != "contracts" else []
