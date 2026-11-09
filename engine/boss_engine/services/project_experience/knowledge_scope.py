"""REPO & KNOWLEDGE FOR THIS APP (BOSSAPP-931).

In a repository that holds several apps, a project's Repo & knowledge tab showed the whole
repository: Hello World (hello-api/ in paramg/boss-sandbox) listed Python, Maven, JavaScript and
Java, the hotspots web-nqueens/app.js and web-stock/app.js, and a graph of nqueens.js and
storage.js. By default the tab now shows the project's own part (project_scope): its stack and
commands, its tests, its code map, its hotspots, and a graph preview centred inside it. The scope
switch ("This app · Whole repo") shows the whole repository as before. With one app per repository
the scope is the whole repository and nothing changes.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from sqlalchemy.engine import Connection

APP, REPO = "app", "repo"


def mode_of(requested: Optional[str], scope: dict[str, Any]) -> str:
    if not scope.get("scoped"):
        return REPO
    return REPO if str(requested or "").lower() in ("repo", "whole", "whole_repo") else APP


def summary(scope: dict[str, Any], mode: str) -> dict[str, Any]:
    roots = scope.get("roots") or []
    return {"scoped": bool(scope.get("scoped")), "mode": mode, "roots": roots,
            "label": ", ".join(f"{r}/" for r in roots) if roots else None,
            "options": ([{"key": APP, "label": "This app"}, {"key": REPO, "label": "Whole repo"}]
                        if scope.get("scoped") else []),
            "others": scope.get("apps") or []}


def _app_paths(connection: Connection, project_id: str, scope: dict[str, Any]
               ) -> tuple[list[str], Optional[Path]]:
    """The app's files (repo-relative) and the kept checkout when one is on disk."""
    from ...config import get_settings
    from .. import project_scope
    from ..ambient import dossier as dossier_service

    checkout: Optional[Path] = None
    try:
        _full, checkout, _files = dossier_service.checkout_for(
            connection, get_settings(), project_id, scope.get("repository"))
    except Exception:  # noqa: BLE001 — no checkout: the graph's file list
        checkout = None
    paths = (dossier_service.list_files(checkout) if checkout is not None
             else project_scope.repo_paths(connection, project_id, scope.get("repository")))
    mine = [p for p in paths if project_scope.in_scope(scope, p)]
    mine += [p for p in project_scope.test_paths_for(scope, paths) if p not in mine]
    return mine, checkout


def _code_map(paths: list[str], roots: list[str]) -> dict[str, Any]:
    """The app's code map: its own folders (hello-api/site), not the repository's."""
    counts: Counter[str] = Counter()
    for p in paths:
        parent = str(PurePosixPath(p).parent)
        root = next((r for r in roots if p.startswith(r.rstrip("/") + "/")), None)
        if root:
            rest = p[len(root) + 1:]
            head = f"{root}/{rest.split('/', 1)[0]}" if "/" in rest else root
        else:
            head = parent if parent != "." else "root"
        counts[head] += 1
    top = [{"dir": d, "files": n} for d, n in counts.most_common(4)]
    rest = len(paths) - sum(d["files"] for d in top)
    if rest > 0:
        top.append({"dir": "other", "files": rest})
    return {"parts": top, "files": len(paths)}


def scope_dossier(connection: Connection, project_id: str, d: dict[str, Any],
                  facts: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    """The dossier the tab shows for THIS app (see the module docstring)."""
    from .. import project_scope
    from ..ambient import dossier as dossier_service
    from ..start_project import _TEST_PATH, detect
    from .knowledge import _hotspots, _stack, commands
    from .test_runs import last_full_run, suite_words

    paths, checkout = _app_paths(connection, project_id, scope)
    roots = scope.get("roots") or []
    cmds: dict[str, list[str]] = {}
    if checkout is not None and len(roots) == 1:
        # Commands from the app's own manifests (hello-api/package.json), said from its folder.
        rel = [p[len(roots[0]) + 1:] for p in paths if p.startswith(roots[0] + "/")]
        own_cmds = dossier_service.commands(checkout / roots[0], rel)
        cmds = {k: [f"cd {roots[0]} && {c}" for c in v] for k, v in own_cmds.items()}
    if not any(cmds.values()):
        said = facts.get("commands") or {}
        cmds = {k: [c for c in v or [] if any(r in str(c) for r in roots)]
                for k, v in said.items()}
    rel_paths = [p[len(roots[0]) + 1:] if len(roots) == 1 and p.startswith(roots[0] + "/") else p
                 for p in paths]
    app_facts = {"stack": detect(rel_paths), "languages": dossier_service.languages(paths),
                 "commands": cmds, "files": len(paths),
                 "tests": {"files": len([p for p in paths if _TEST_PATH.search(p)])},
                 "ci": facts.get("ci") or [], "docs": facts.get("docs") or {},
                 "todos": {"total": sum(int(c.get("count") or 0) for c in
                                        (facts.get("todos") or {}).get("clusters") or []
                                        if project_scope.in_scope(scope, c.get("dir")))},
                 "hotspots": [h for h in facts.get("hotspots") or []
                              if project_scope.in_scope(scope, h.get("file"))],
                 "advisories": {}}
    run = last_full_run(connection, project_id)
    tests = dict(d.get("tests") or {})
    tests["files"] = app_facts["tests"]["files"]
    tests["line"] = suite_words(run, tests["files"])["line"]
    return {**d, "stack": _stack(app_facts), "commands": commands(app_facts),
            "tests": tests, "files": len(paths), "code_map": _code_map(paths, roots),
            "hotspots": _hotspots(app_facts),
            "todo_clusters": [c for c in d.get("todo_clusters") or []
                              if project_scope.in_scope(scope, c.get("dir"))],
            "highlights": dossier_service.highlights(app_facts),
            "scope": APP}
