"""A deleted project's files on THIS machine (Delete project, BOSSAPP-915 follow-up).

projects_delete.purge takes every row; this takes what BOSS keeps on disk for the project, after
the rows are gone (`delete_project` calls `collect` before the transaction and `remove` after it
commits, so a rolled-back delete removes nothing):

  * a local project's bare repositories   <repo root>/<project_id>/          (local_projects)
  * a GitHub repository's outage mirror   <repo root>/_mirrors/<owner>/<repo>.git
      — only when no other project binds that repository, builds on it or waits to push to it
  * retained build workspaces             <sandbox root>/<run_id>-*, the agent shell's
      <BOSS_AGENT_WORKSPACE_ROOT>/<run_id>, a sandbox uid's <run root>/<owner_key(run or preview)>,
      a lineage's warm sandbox (lineage_sandboxes.run_dir)
  * preview trees and logs                <preview root>/<preview_id>-*, <preview_id>.log,
      _logs/<preview_id>.log, app_previews.log_path
  * artifacts                             <artifact root>/<project_id>/ (artifacts, records, media),
      <artifact root>/mobile/<sha256(project)[:24]>/
  * the knowledge index's checkouts       <kg index dir>/<binding_id>
  * the Java toolchain cache              <toolchain cache>/<sha256(project)[:32]>

ONLY UNDER BOSS-OWNED ROOTS. Every path is checked against the roots above before anything is
removed: a path outside them (a log_path or run_dir a row pointed elsewhere, a symlink out) is
refused and reported, never followed. A root itself is never removed. Best effort per path: the
project is already gone from the database, so a file that will not go is reported, not raised.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterable, Optional

from sqlalchemy import text

log = logging.getLogger(__name__)

__all__ = ["owned_roots", "collect", "remove"]


def _sandbox_root(settings: Any) -> Path:
    configured = str(getattr(settings, "sandbox_root_path", "") or "")
    return Path(configured).expanduser() if configured else Path(tempfile.gettempdir()) / "boss-sandboxes"


def _artifact_root(settings: Any) -> Path:
    return Path(settings.artifact_root)


def _kg_root(settings: Any) -> Path:
    configured = str(getattr(settings, "kg_index_dir", "") or "")
    return Path(configured) if configured else Path(tempfile.gettempdir()) / "boss-kg-index"


def owned_roots(settings: Any) -> dict[str, Path]:
    """Every directory BOSS owns that can hold a project's files, by name."""
    from . import agent_shell, local_repo, preview_service, sandbox_identity, toolchains

    return {
        "repos": local_repo.root(settings),
        "artifacts": _artifact_root(settings),
        "previews": preview_service.PREVIEW_ROOT,
        "sandboxes": _sandbox_root(settings),
        "sandbox_runs": sandbox_identity.run_root(),
        "agent_workspaces": agent_shell.WORKSPACE_ROOT,
        "lineage_sandboxes": Path("/var/lib/boss/lineage-sandboxes"),
        "kg_index": _kg_root(settings),
        "toolchain_cache": toolchains.cache_root(),
    }


def _rows(c: Any, sql: str, params: dict) -> list:
    try:
        with c.begin_nested():
            return list(c.execute(text(sql), params).all())
    except Exception:  # noqa: BLE001 — an install without that table has nothing of it on disk
        log.debug("project files: %s skipped", sql.split()[3] if len(sql.split()) > 3 else sql,
                  exc_info=True)
        return []


def _shared_elsewhere(c: Any, project_id: str, owner: str, repo: str) -> bool:
    """Another project binds, builds on, or waits to push to owner/repo: its mirror stays."""
    p = {"p": project_id, "o": owner.lower(), "r": repo.lower()}
    for sql in (
        "SELECT 1 FROM github_repository_bindings WHERE project_id <> :p AND lower(owner) = :o "
        "AND lower(name) = :r LIMIT 1",
        "SELECT 1 FROM code_task_runs WHERE project_id <> :p AND lower(spec->>'owner') = :o "
        "AND lower(spec->>'repo') = :r LIMIT 1",
        "SELECT 1 FROM repo_push_pending WHERE project_id <> :p AND lower(owner) = :o "
        "AND lower(repo) = :r AND status IN ('waiting', 'pushing') LIMIT 1",
    ):
        if _rows(c, sql, p):
            return True
    return False


def collect(c: Any, settings: Any, project_id: str) -> dict[str, Any]:
    """What to remove, read from the project's rows BEFORE they are deleted."""
    from . import local_repo, preview_service, sandbox_identity, toolchains, agent_shell

    roots = owned_roots(settings)
    exact: list[Path] = []      # remove this path
    patterns: list[tuple[Path, str]] = []   # remove what matches glob under that root

    exact.append(local_repo.project_dir(project_id, settings))
    exact.append(roots["artifacts"] / project_id)
    exact.append(roots["artifacts"] / "mobile" / hashlib.sha256(project_id.encode()).hexdigest()[:24])
    exact.append(toolchains.project_cache_dir(project_id))

    run_ids = [r[0] for r in _rows(c, "SELECT id FROM code_task_runs WHERE project_id = :p",
                                    {"p": project_id})]
    for rid in run_ids:
        rid = str(rid)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", rid):
            continue
        patterns.append((roots["sandboxes"], f"{rid}-*"))
        exact.append(agent_shell.workspace_for(rid))
        exact.append(sandbox_identity.run_root() / sandbox_identity.owner_key(rid))
    for (run_dir,) in _rows(c, "SELECT run_dir FROM lineage_sandboxes WHERE project_id = :p",
                            {"p": project_id}):
        if run_dir:
            exact.append(Path(str(run_dir)))
    for pid, log_path in _rows(c, "SELECT id, log_path FROM app_previews WHERE project_id = :p",
                               {"p": project_id}):
        pid = str(pid)
        if re.fullmatch(r"[A-Za-z0-9_.-]+", pid):
            patterns.append((roots["previews"], f"{pid}-*"))
            exact.append(Path(preview_service.PREVIEW_ROOT) / f"{pid}.log")
            exact.append(Path(preview_service.PREVIEW_ROOT) / "_logs" / f"{pid}.log")
            exact.append(sandbox_identity.run_root() / sandbox_identity.owner_key(pid))
        if log_path:
            exact.append(Path(str(log_path)))
    mirrors = []
    for bid, owner, name, host in _rows(
            c, "SELECT id, owner, name, COALESCE(host, 'github') FROM github_repository_bindings "
               "WHERE project_id = :p", {"p": project_id}):
        exact.append(roots["kg_index"] / str(bid))
        if str(host) == "local" or local_repo.is_local_owner(owner):
            continue
        if not _shared_elsewhere(c, project_id, str(owner), str(name)):
            try:
                exact.append(local_repo.mirror_path(str(owner), str(name), settings))
                mirrors.append(f"{owner}/{name}")
            except local_repo.LocalRepoError:
                pass
    return {"project_id": project_id, "exact": [str(p) for p in exact],
            "patterns": [(str(r), g) for r, g in patterns], "mirrors": mirrors}


def _inside(path: Path, roots: Iterable[Path]) -> bool:
    """`path` (its parent resolved, the last part as written) is strictly inside a root."""
    try:
        resolved = path.parent.resolve() / path.name
    except OSError:
        return False
    if path.name in ("", ".", ".."):
        return False
    for root in roots:
        try:
            base = root.resolve()
        except OSError:
            continue
        if resolved != base and base in resolved.parents:
            return True
    return False


def _holds_a_root(path: Path, roots: Iterable[Path]) -> bool:
    """`path` is, or contains, one of BOSS's roots (artifacts/<id> where the id is "repos")."""
    try:
        resolved = path.resolve()
    except OSError:
        return True
    for root in roots:
        try:
            base = root.resolve()
        except OSError:
            continue
        if base == resolved or resolved in base.parents:
            return True
    return False


def _remove_one(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def remove(settings: Any, plan: dict[str, Any]) -> dict[str, Any]:
    """Remove what `collect` found, only under BOSS's roots. Never raises."""
    from . import local_projects

    roots = list(owned_roots(settings).values())
    out: dict[str, Any] = {"removed": [], "refused": [], "failed": [], "local_repo": False}
    try:
        out["local_repo"] = bool(local_projects.delete_local_repo(plan["project_id"], settings))
    except Exception as error:  # noqa: BLE001
        out["failed"].append({"path": "local repository", "error": str(error)[:200]})
    candidates: list[Path] = [Path(p) for p in plan.get("exact") or []]
    for root, pattern in plan.get("patterns") or []:
        base = Path(root)
        if base.is_dir():
            candidates.extend(base.glob(pattern))
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if not os.path.lexists(path):
            continue
        if not _inside(path, roots) or _holds_a_root(path, roots):
            out["refused"].append(key)
            log.warning("delete project %s: refused %s, outside BOSS's directories",
                        plan.get("project_id"), key)
            continue
        try:
            _remove_one(path)
            out["removed"].append(key)
        except OSError as error:
            out["failed"].append({"path": key, "error": str(error)[:200]})
    return out


def describe(result: Optional[dict[str, Any]]) -> dict[str, Any]:
    """The counts the audit entry carries (paths stay in the log, not the audit row)."""
    result = result or {}
    return {"removed": len(result.get("removed") or []), "refused": len(result.get("refused") or []),
            "failed": len(result.get("failed") or []), "local_repo": bool(result.get("local_repo"))}
