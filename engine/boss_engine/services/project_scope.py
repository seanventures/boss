"""A PROJECT'S PART OF ITS REPOSITORY (BOSSAPP-931).

The operator, walking "Hello World" (paramg/boss-sandbox holds a hello REST API, web-nqueens,
sudoku, web-stock, stock_trading_core and more): "If repo has multiple projects we can probably
first display the recommendation for the project in that repo while other repo recommendations can
go in a separate section… the Hello World project containing the recommendation of nqueen app does
not make any sense to me… Same thing for repo and knowledge graph".

So every view that reads the repository asks here what is THIS project's:

* ``roots`` — the app's folders: its code components (app_components, Factory › Apps ›
  Components; onboarding's component mapping), the folder BOSS recorded for its builds
  (finalize_ctx.app_dir), else the folder its own builds wrote into.
* ``files`` — every file the project's builds wrote (tests in a shared ``tests/`` folder included).
* ``scoped`` — True only when the repository holds OTHER apps beside this one. With one app per
  repository nothing is scoped and nothing visible changes.

:func:`place` sorts a path: ``app`` (this project's), ``elsewhere`` (another app in the same
repository, named by the project that owns it when one does, else by its folder) or ``repo``
(nothing in particular: the whole repository, e.g. CI).
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import PurePosixPath
from typing import Any, Iterable, Optional

from sqlalchemy import text

#: Top-level folders that are never an app of their own.
NOT_APPS = frozenset({"tests", "test", "__tests__", "spec", "docs", "doc", ".github", ".circleci",
                      "scripts", "script", "tools", "bin", "ci", ".vscode", ".idea", "node_modules",
                      ".boss", "vendor", "third_party", "examples"})
ROOT_LABEL = "Repo root"


def _j(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value if value is not None else default


def _clean(value: Any) -> Optional[str]:
    from .app_root import clean_rel

    return clean_rel(value)


def primary_repo(connection, project_id: str) -> Optional[str]:
    return connection.execute(text(
        "SELECT full_name FROM github_repository_bindings WHERE project_id = :p AND "
        "status = 'active' ORDER BY (role = 'primary') DESC, created_at LIMIT 1"),
        {"p": project_id}).scalar()


def _top(path: str) -> str:
    return path.split("/", 1)[0] if "/" in path else "."


def _run_paths(connection, project_id: str, repo_name: str) -> tuple[list[str], list[str]]:
    """(folders BOSS recorded for the project's builds, every file its builds wrote)."""
    recorded: list[str] = []
    files: list[str] = []
    rows = connection.execute(text(
        "SELECT state, finalize_ctx FROM code_task_runs WHERE project_id = :p "
        "AND lower(COALESCE(spec->>'repo', '')) = lower(:r) ORDER BY created_at DESC LIMIT 200"),
        {"p": project_id, "r": repo_name}).mappings().all()
    from .app_root import _paths_of  # noqa: PLC2701 — the one reader of a run's written files

    for row in rows:
        fctx = _j(row["finalize_ctx"], {}) or {}
        state = _j(row["state"], {}) or {}
        d = _clean(fctx.get("app_dir"))
        if d and d not in recorded:
            recorded.append(d)
        for p in _paths_of({"state": state, "finalize_ctx": fctx}):
            p = str(p).strip().lstrip("./")
            if p and p not in files:
                files.append(p)
    return recorded, files


def _components(connection, project_id: str, full_name: str) -> list[str]:
    try:
        rows = connection.execute(text(
            "SELECT c.path FROM app_components c JOIN github_repository_bindings b "
            "ON b.id = c.binding_id WHERE c.project_id = :p AND c.kind = 'code' "
            "AND lower(b.full_name) = lower(:r) ORDER BY c.position, c.name"),
            {"p": project_id, "r": full_name}).scalars().all()
    except Exception:  # noqa: BLE001 — no components table: the runs decide
        return []
    return [d for d in (_clean(r) for r in rows) if d]


def _roots_from_files(files: Iterable[str]) -> list[str]:
    """The folder(s) the builds wrote into: the top-level app folders their code files sit in
    (never tests/ or docs/, never root manifests a build touched on the way)."""
    counts = Counter(_top(p) for p in files if _top(p) not in NOT_APPS and _top(p) != ".")
    if not counts:
        return []
    most = counts.most_common(1)[0][1]
    # A second folder counts when it holds a real share of the work (a web/ beside an api/).
    return [d for d, n in counts.most_common() if n >= max(2, most // 3) or n == most]


def repo_paths(connection, project_id: str, full_name: Optional[str] = None) -> list[str]:
    """The repository's files as BOSS knows them: the knowledge graph's file nodes."""
    rows = connection.execute(text(
        "SELECT DISTINCT path FROM kg_nodes WHERE project_id = :p AND kind = 'file' "
        "AND valid_to IS NULL AND path IS NOT NULL AND (CAST(:r AS TEXT) IS NULL OR repo = :r "
        "OR repo IS NULL OR repo = '') LIMIT 20000"),
        {"p": project_id, "r": full_name}).scalars().all()
    return [str(p) for p in rows if p]


def _app_folders(paths: Iterable[str], structure: Iterable[Any] = ()) -> list[str]:
    """Top-level folders in the repository that hold code (candidates for other apps)."""
    from .start_project import _EXT_LANG

    counts: Counter[str] = Counter()
    for p in paths:
        if "/" in p and PurePosixPath(p).suffix.lower() in _EXT_LANG:
            counts[_top(p)] += 1
    for d in structure or []:
        if isinstance(d, dict) and d.get("dir") and d["dir"] != "." and int(d.get("files") or 0) >= 2:
            counts[str(d["dir"])] += 0 if counts[str(d["dir"])] else 1
    return [d for d, _ in counts.most_common() if d not in NOT_APPS]


def _structure(connection, project_id: str, full_name: str) -> list[Any]:
    try:
        facts = connection.execute(text(
            "SELECT facts FROM project_dossiers WHERE project_id = :p AND repo = :r"),
            {"p": project_id, "r": full_name}).scalar()
    except Exception:  # noqa: BLE001
        return []
    return list((_j(facts, {}) or {}).get("structure") or [])


def own(connection, project_id: str, full_name: Optional[str] = None) -> dict[str, Any]:
    """{repository, roots, files} for one project, with no look at its neighbours."""
    full_name = full_name or primary_repo(connection, project_id)
    if not full_name:
        return {"repository": None, "roots": [], "files": []}
    repo_name = full_name.split("/", 1)[-1]
    recorded, files = _run_paths(connection, project_id, repo_name)
    roots = _components(connection, project_id, full_name) or recorded or _roots_from_files(files)
    return {"repository": full_name, "roots": roots, "files": files}


def _neighbours(connection, project_id: str, full_name: str) -> list[dict[str, Any]]:
    """The other projects bound to the same repository, each with its own app folders."""
    rows = connection.execute(text(
        "SELECT DISTINCT b.project_id, p.name FROM github_repository_bindings b JOIN projects p "
        "ON p.id = b.project_id WHERE lower(b.full_name) = lower(:r) AND b.status = 'active' "
        "AND b.project_id <> :p AND p.archived_at IS NULL ORDER BY p.name"),
        {"r": full_name, "p": project_id}).mappings().all()
    out = []
    for r in rows:
        mine = own(connection, str(r["project_id"]), full_name)
        if mine["roots"] or mine["files"]:
            out.append({"project_id": str(r["project_id"]), "name": str(r["name"] or r["project_id"]),
                        "roots": mine["roots"], "files": mine["files"]})
    return out


def of(connection, project_id: str) -> dict[str, Any]:
    """The project's scope in its primary repository (see the module docstring)."""
    mine = own(connection, project_id)
    full_name = mine["repository"]
    if not full_name or not mine["roots"]:
        return {**mine, "scoped": False, "apps": [], "neighbours": []}
    neighbours = _neighbours(connection, project_id, full_name)
    paths = repo_paths(connection, project_id, full_name)
    others = [d for d in _app_folders(paths, _structure(connection, project_id, full_name))
              if d not in mine["roots"]]
    scoped = any(n["roots"] for n in neighbours) or _shared(connection, project_id, full_name,
                                                             mine, others, paths)
    return {**mine, "scoped": scoped, "apps": others if scoped else [], "neighbours": neighbours}


def _shared(connection, project_id: str, full_name: str, mine: dict[str, Any],
            others: list[str], paths: list[str]) -> bool:
    """Does the repository hold OTHER apps beside this project's? The checkout answers when it
    is on disk (app_root.is_multi_app: three or more app folders, or a workspaces root). Else the
    file list: two or more other code folders, and this app is the smaller part of the code — a
    full-stack app's server/ + web/ + infra/ written by its own builds is one app, not three."""
    try:
        from ..config import get_settings
        from . import app_root
        from .ambient import dossier as dossier_service

        _full, checkout, _files = dossier_service.checkout_for(connection, get_settings(),
                                                                project_id, full_name)
        if checkout is not None:
            return app_root.is_multi_app(checkout)
    except Exception:  # noqa: BLE001 — no checkout: the file list decides
        pass
    if len(others) < 2:
        return False
    from .start_project import _EXT_LANG

    code = [p for p in paths if PurePosixPath(p).suffix.lower() in _EXT_LANG]
    if not code:
        return True
    scope = {**mine, "scoped": True}
    ours = sum(1 for p in code if in_scope(scope, p))
    return ours * 2 < len(code)


def _under(path: str, roots: Iterable[str]) -> bool:
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def looks_like_path(value: Any) -> bool:
    raw = str(value or "").strip()
    if not raw or " " in raw or len(raw) > 300:
        return False
    return "/" in raw or bool(PurePosixPath(raw).suffix)


def place(scope: dict[str, Any], path: Any) -> dict[str, Any]:
    """Where a path sits for this project: {"where": "app" | "elsewhere" | "repo", "app",
    "project"}. An unscoped project's every path is its own."""
    raw = str(path or "").strip().lstrip("./") if path else ""
    if not scope.get("scoped"):
        return {"where": "app" if raw else "repo", "app": None, "project": None}
    if not raw:
        return {"where": "repo", "app": None, "project": None}
    if raw in (scope.get("files") or []) or _under(raw, scope.get("roots") or []):
        return {"where": "app", "app": None, "project": None}
    for n in scope.get("neighbours") or []:
        if raw in n["files"] or _under(raw, n["roots"]):
            app = next((r for r in n["roots"] if _under(raw, [r])), None) or n["name"]
            return {"where": "elsewhere", "app": app,
                    "project": {"id": n["project_id"], "name": n["name"]}}
    top = _top(raw)
    return {"where": "elsewhere", "app": ROOT_LABEL if top == "." else top, "project": None}


def in_scope(scope: dict[str, Any], path: Any) -> bool:
    return place(scope, path)["where"] == "app"


def test_paths_for(scope: dict[str, Any], paths: Iterable[str]) -> list[str]:
    """The test files of this app outside its folders: the ones its builds wrote, and a shared
    tests/ file named after one of the app's own files (tests/test_greeting.py → greeting.py)."""
    from .start_project import _TEST_PATH

    stems = {PurePosixPath(p).stem.lower() for p in paths if in_scope(scope, p)
             and not _TEST_PATH.search(p)}
    stems = {s for s in stems if len(s) >= 4 and s not in ("index", "main", "app", "init")}
    out = []
    for p in paths:
        if not _TEST_PATH.search(p) or in_scope(scope, p):
            continue
        name = PurePosixPath(p).name.lower()
        if any(s in name for s in stems):
            out.append(p)
    return out
