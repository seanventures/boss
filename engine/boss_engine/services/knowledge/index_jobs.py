"""Keeping the code graph current: the `knowledge.index` job (KNOWLEDGE_GRAPH_SPEC.md §3.3, Phase 1).

When it runs:

* **Every push BOSS makes.** After a governed code task opens or updates its PR, the run enqueues
  a job for that repository with the PR's branch.
* **Hourly**, an incremental pass over every indexed repository's default branch. It picks up
  merges without hooking into the merge path, and it is a no-op when nothing moved.
* **Nightly**, a full reconcile per repository.

What a job does:

1. Brings the repository's partial clone up to date (`--filter=blob:none`: all history, and
   file contents only as git needs them). The token rides in git's environment only, never in a
   URL or `.git/config` (IDENTITY.md §7.10).
2. Indexes the default branch, incrementally from the last indexed commit, or fully. The graph
   describes the default branch, not unmerged work.
3. For a push, records which symbols the PR changed: its diff hunks mapped onto definitions.
   These are stored on the run (`code_task_runs.state.changed_symbols`).
4. Re-syncs the lifecycle graph, so the PR, its spec, decision and changed symbols are joined.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from .code_index import CodeIndexer, indexable, symbol_key
from .code_parse import parse_file
from .lifecycle import LifecycleGraph
from .store import Scope

JOB_KIND = "knowledge.index"
FULL_EVERY_HOURS = 24
INCREMENTAL_EVERY_MINUTES = 60
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)


def cache_root() -> Path:
    base = os.environ.get("BOSS_REPO_CACHE") or os.path.join(tempfile.gettempdir(), "boss-repo-cache")
    return Path(base) / "knowledge"


def enqueue_index(connection: Connection, project_id: str, repo: str, *, reason: str, mode: str = "incremental",
                  ref: str | None = None, base: str | None = None, run_id: str | None = None,
                  pr_number: int | None = None) -> dict[str, Any]:
    """Queue one index job. Idempotent per (repo, mode, ref, run) and, for scheduled work, per window."""
    from ..jobs import enqueue_job

    window = datetime.now(timezone.utc).strftime("%Y%m%d" if mode == "full" else "%Y%m%d%H")
    dedupe = f"kg-index:{repo}:{mode}:{ref or 'default'}:{run_id or window}"
    payload = {"repo": repo, "mode": mode, "reason": reason, "ref": ref, "base": base, "run_id": run_id,
               "pr_number": pr_number}
    return enqueue_job(connection, project_id, JOB_KIND, payload, priority=150, max_attempts=2, dedupe_key=dedupe)


def enqueue_due(connection: Connection) -> int:
    """The scheduled half: a full reconcile per repository once a day, an incremental pass hourly."""
    rows = connection.execute(text(
        "SELECT project_id, repo, last_full_at, updated_at FROM kg_code_index_state")).mappings().all()
    now = datetime.now(timezone.utc)
    queued = 0
    for r in rows:
        full_due = r["last_full_at"] is None or (now - r["last_full_at"]).total_seconds() > FULL_EVERY_HOURS * 3600
        inc_due = (now - r["updated_at"]).total_seconds() > INCREMENTAL_EVERY_MINUTES * 60
        if full_due or inc_due:
            enqueue_index(connection, r["project_id"], r["repo"], reason="schedule",
                          mode="full" if full_due else "incremental")
            queued += 1
    return queued


def _git(root: Path, *args: str, env: dict | None = None, check: bool = True) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=900, check=check,
                          env={**os.environ, **(env or {})}).stdout.strip()


def checkout(repo: str, *, env: dict, url: str, refs: tuple[str, ...] = ()) -> Path:
    """A partial clone of `repo` with the default branch (and `refs`) fetched; returns its path."""
    owner, _, name = repo.partition("/")
    dest = cache_root() / f"{owner}__{name}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not (dest / ".git").exists():
        subprocess.run(["git", "clone", "--filter=blob:none", "--no-checkout", url, str(dest)], check=True,
                       capture_output=True, timeout=1800, env={**os.environ, **env})
    else:
        _git(dest, "remote", "set-url", "origin", url)
    _git(dest, "fetch", "--filter=blob:none", "origin", "+HEAD:refs/remotes/origin/HEAD", env=env)
    for ref in refs:
        _git(dest, "fetch", "--filter=blob:none", "origin", f"+{ref}:refs/remotes/origin/kg/{ref}", env=env)
    _git(dest, "checkout", "--quiet", "--detach", "--force", "refs/remotes/origin/HEAD", env=env)
    return dest


def changed_symbols(root: Path, repo: str, base: str, head: str, *, env: dict | None = None) -> list[str]:
    """Symbols a change touched: definitions on the head side whose lines a hunk covers (the innermost
    one), plus definitions the change removed (keyed as they were on the base side)."""
    out: set[str] = set()
    mb = _git(root, "merge-base", base, head, env=env)
    for path in _git(root, "diff", "--name-only", mb, head, env=env).splitlines():
        if not indexable(path):
            continue
        new_src = subprocess.run(["git", "show", f"{head}:{path}"], cwd=root, capture_output=True,
                                 env={**os.environ, **(env or {})})
        old_src = subprocess.run(["git", "show", f"{mb}:{path}"], cwd=root, capture_output=True,
                                 env={**os.environ, **(env or {})})
        new_defs = parse_file(path, new_src.stdout).defs if new_src.returncode == 0 else []
        old_defs = parse_file(path, old_src.stdout).defs if old_src.returncode == 0 else []
        for m in HUNK.finditer(_git(root, "diff", "-U0", mb, head, "--", path, env=env)):
            start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            if count:
                covering = [d for d in new_defs if d.line <= start + count - 1 and d.end_line >= start]
            else:
                # A pure deletion ("+c,0") sits between lines c and c+1: it changed a definition
                # only if that definition spans both sides of the gap.
                covering = [d for d in new_defs if d.line <= start and d.end_line >= start + 1]
            innermost = [d for d in covering if not any(o is not d and d.line <= o.line and o.end_line <= d.end_line
                                                        for o in covering)]
            out.update(symbol_key(repo, path, d.qualname) for d in innermost)
        removed = {d.qualname for d in old_defs} - {d.qualname for d in new_defs}
        out.update(symbol_key(repo, path, q) for q in removed)
    return sorted(out)


def run_index_job(engine: Engine, settings: Any, project_id: str, payload: dict) -> dict[str, Any]:
    """Execute one `knowledge.index` job (the worker asks the API to run it here)."""
    from ..github_connections import github_token_for
    from ..repo_host import git_access

    repo = payload["repo"]
    owner, _, name = repo.partition("/")
    scope = Scope(project_id)
    with engine.begin() as c:
        token = github_token_for(c, settings, project_id)
    access = git_access(getattr(settings, "target_repo_host", "github") or "github", owner, name, token)
    # BOSS's own git runs as the control plane's uid: boss_environ() carries the credential (repo_host.GitAccess).
    env = access.boss_environ()
    ref, base = payload.get("ref"), payload.get("base")
    root = checkout(repo, env=env, url=access.url, refs=tuple(r for r in (ref, base) if r))
    head = _git(root, "rev-parse", "refs/remotes/origin/HEAD")
    indexer = CodeIndexer()
    with engine.begin() as c:
        run = (indexer.index_full(c, scope, repo, root, head) if payload.get("mode") == "full"
               else indexer.index_incremental(c, scope, repo, root, head))
    result: dict[str, Any] = {"index": run}
    if ref and payload.get("run_id"):
        base_ref = f"refs/remotes/origin/kg/{base}" if base else "refs/remotes/origin/HEAD"
        keys = changed_symbols(root, repo, base_ref, f"refs/remotes/origin/kg/{ref}", env=env)
        with engine.begin() as c:
            c.execute(text("UPDATE code_task_runs SET state = jsonb_set(COALESCE(state, '{}'::jsonb), "
                           "'{changed_symbols}', CAST(:k AS JSONB)) WHERE id = :r AND project_id = :p"),
                      {"k": _json(keys), "r": payload["run_id"], "p": project_id})
        result["changed_symbols"] = len(keys)
    with engine.begin() as c:
        result["lifecycle"] = LifecycleGraph().sync(c, scope)
    return result


def _json(value: Any) -> str:
    import json

    return json.dumps(value)
