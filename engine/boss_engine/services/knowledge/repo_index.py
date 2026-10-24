"""The code graph of every registered repository.

BOSS's own agent found ``kg_nodes`` empty in three projects that each had a registered
repository: the graph was fed only by the code BOSS itself wrote (``capture`` after a build) and
by finished runs and incidents, and ``code_index.index_repository`` had no caller. A project's
repositories are what its agents read most, so the graph starts from them.

How it runs (one implementation; the worker, the API route, the console and the agent tool all
call into it):

* ``kg_repo_index`` holds one row per project repository: what was indexed (commit, files,
  nodes, edges, time) and whether an index is ``due``. Registering a repository, or moving its
  base branch, marks it due through a database trigger, whatever code path did it.
* Every knowledge tick (the worker, every 30 s) ``enqueue_due`` queues one governed
  ``knowledge.repository.index`` job per due row (deduplicated: one in flight per repository).
  The sweep tick (every 10 minutes) also catches what the trigger cannot see: a repository
  registered before this existed, and a base branch whose head may have moved since the last
  look (``BOSS_KG_INDEX_RECHECK_MINUTES``; a newer head in github_repository_ref_states counts
  at once).
* The worker hands the leased job to the API (``execute_job``), which fetches the base branch
  through BOSS's own clone path (``identity.runs.repo_access``: the project's GitHub connection,
  the egress proxy when on; the credential rides in git's environment only) into a shallow
  checkout kept between runs, and indexes it in a CHILD PROCESS: memory is returned when it
  ends, and the timeout kills it. A head that did not move is not indexed again. A head that
  moved by a few files is indexed incrementally (``index_files`` for what changed, the deleted
  files' nodes closed); otherwise the whole tree (``index_repository``, which re-parses only
  files whose content changed).
* Bounded: files, source bytes, one file's size and the time are settings
  (``BOSS_KG_INDEX_*``). Idempotent: an index of the same tree changes nothing. A failed or
  killed run writes nothing (one transaction) and the previous graph stays.
* Every run ends in a receipt (``knowledge.repository.index``) and an event. No model is called:
  this is parsing; embeddings follow through the budgeted ``embed_due`` tick.

The project's ``knowledge_graph`` switch governs what agents are GIVEN, never what is captured:
a project with the switch off is indexed too (the case study compares arms on the same graph).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

JOB_KIND = "knowledge.repository.index"
EFFECT_KIND = "knowledge.repository.index"
#: Jobs queued per tick, across projects: a burst of registrations drains over a few ticks.
ENQUEUE_PER_TICK = 5
#: A moved head with at most this many changed files is indexed incrementally.
INCREMENTAL_MAX_FILES = 300
_LIVE_JOB = ("queued", "leased", "running", "failed_retryable")
_NUMBERS = ("files", "parsed", "nodes", "edges")


@dataclass(frozen=True)
class Limits:
    enabled: bool = True
    max_files: int = 30000
    max_file_bytes: int = 1_000_000
    max_total_bytes: int = 400_000_000
    timeout_seconds: int = 1800
    recheck_minutes: int = 60
    work_dir: str = ""

    @classmethod
    def from_settings(cls, settings: Any) -> "Limits":
        def num(name: str, default: int) -> int:
            try:
                return int(getattr(settings, name, default))
            except (TypeError, ValueError):
                return default

        return cls(enabled=bool(num("kg_repo_index", 1)),
                   max_files=num("kg_index_max_files", cls.max_files),
                   max_file_bytes=num("kg_index_max_file_bytes", cls.max_file_bytes),
                   max_total_bytes=num("kg_index_max_total_bytes", cls.max_total_bytes),
                   timeout_seconds=num("kg_index_timeout_seconds", cls.timeout_seconds),
                   recheck_minutes=num("kg_index_recheck_minutes", cls.recheck_minutes),
                   work_dir=str(getattr(settings, "kg_index_dir", "") or ""))


# --- what is due --------------------------------------------------------------------------------
def observe_bindings(connection: Connection, project_id: Optional[str] = None) -> int:
    """Rows for active repositories that have none (registered before the trigger existed), and
    due again where the binding's base branch differs from the one last indexed."""
    params = {"p": project_id}
    added = connection.execute(text(
        """
        INSERT INTO kg_repo_index (project_id, binding_id, repo, branch, due, reason)
        SELECT b.project_id, b.id, b.full_name, COALESCE(b.default_branch, ''), true, 'registered'
        FROM github_repository_bindings b
        WHERE b.status = 'active' AND (CAST(:p AS TEXT) IS NULL OR b.project_id = :p)
        ON CONFLICT (project_id, binding_id) DO NOTHING
        """), params).rowcount
    moved = connection.execute(text(
        """
        UPDATE kg_repo_index r SET branch = COALESCE(b.default_branch, ''), repo = b.full_name,
               due = true, reason = 'branch_changed', updated_at = now()
        FROM github_repository_bindings b
        WHERE b.id = r.binding_id AND b.status = 'active' AND NOT r.due
          AND (CAST(:p AS TEXT) IS NULL OR r.project_id = :p)
          AND (r.branch IS DISTINCT FROM COALESCE(b.default_branch, '') OR r.repo <> b.full_name)
        """), params).rowcount
    return int(added or 0) + int(moved or 0)


def mark_stale(connection: Connection, *, recheck_minutes: int, project_id: Optional[str] = None) -> int:
    """Due again: a newer head observed for the base branch, or no look for `recheck_minutes`
    (a failed run is retried on the same clock)."""
    params = {"p": project_id, "m": max(1, int(recheck_minutes))}
    moved = connection.execute(text(
        """
        UPDATE kg_repo_index r SET due = true, reason = 'head_moved', updated_at = now()
        FROM github_repository_ref_states s
        WHERE s.repository_binding_id = r.binding_id AND s.ref_name IN (r.branch, 'refs/heads/' || r.branch)
          AND s.commit_sha IS NOT NULL AND s.commit_sha IS DISTINCT FROM r.indexed_sha
          AND NOT r.due AND r.status IN ('indexed', 'failed')
          AND (CAST(:p AS TEXT) IS NULL OR r.project_id = :p)
        """), params).rowcount
    stale = connection.execute(text(
        """
        UPDATE kg_repo_index r SET due = true, reason = 'recheck', updated_at = now()
        WHERE NOT r.due AND r.status IN ('indexed', 'failed')
          AND COALESCE(r.last_checked_at, r.updated_at) < now() - make_interval(mins => :m)
          AND (CAST(:p AS TEXT) IS NULL OR r.project_id = :p)
        """), params).rowcount
    # A job that ended without reporting (deadlettered, cancelled, its worker gone) leaves the
    # row queued or running with nothing in flight: due again.
    orphaned = connection.execute(text(
        """
        UPDATE kg_repo_index r SET due = true, reason = 'retry', updated_at = now()
        WHERE NOT r.due AND r.status IN ('queued', 'running')
          AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.id = r.job_id AND j.status = ANY(:live))
          AND (CAST(:p AS TEXT) IS NULL OR r.project_id = :p)
        """), {"p": project_id, "live": list(_LIVE_JOB)}).rowcount
    return int(moved or 0) + int(stale or 0) + int(orphaned or 0)


def _in_flight(connection: Connection, row: dict) -> bool:
    if row["status"] not in ("queued", "running") or not row.get("job_id"):
        return False
    status = connection.execute(text("SELECT status FROM jobs WHERE id = :j"), {"j": row["job_id"]}).scalar()
    return status in _LIVE_JOB


def _enqueue(connection: Connection, row: dict, *, priority: int) -> Optional[str]:
    from ..jobs import enqueue_job

    job = enqueue_job(
        connection, row["project_id"], JOB_KIND,
        {"binding_id": row["binding_id"], "repo": row["repo"], "reason": row["reason"],
         "force": bool(row.get("force"))},
        priority=priority, max_attempts=2,
        dedupe_key=f"kg-index:{row['binding_id']}:{os.urandom(6).hex()}",
        data_boundary="internal")
    if not job.get("id"):
        return None
    connection.execute(text(
        "UPDATE kg_repo_index SET status = 'queued', due = false, job_id = :j, error = NULL, "
        "updated_at = now() WHERE project_id = :p AND binding_id = :b"),
        {"j": job["id"], "p": row["project_id"], "b": row["binding_id"]})
    return job["id"]


def _due_rows(connection: Connection, *, project_id: Optional[str], limit: int,
              bindings: Optional[list[str]] = None) -> list[dict]:
    return [dict(r) for r in connection.execute(text(
        """
        SELECT r.* FROM kg_repo_index r
        JOIN github_repository_bindings b ON b.id = r.binding_id AND b.status = 'active'
        WHERE r.due AND (CAST(:p AS TEXT) IS NULL OR r.project_id = :p)
          AND (CAST(:bs AS TEXT[]) IS NULL OR r.binding_id = ANY(CAST(:bs AS TEXT[])))
        ORDER BY r.updated_at LIMIT :n FOR UPDATE OF r SKIP LOCKED
        """), {"p": project_id, "n": limit, "bs": bindings}).mappings().all()]


def enqueue_due(engine: Any, settings: Any = None, *, project_id: Optional[str] = None,
                sweep: bool = False, limit: int = ENQUEUE_PER_TICK) -> dict[str, int]:
    """The worker tick: queue an index job for each due repository (one in flight per
    repository). On a sweep, first mark what may have moved."""
    from ..jobs import JOB_PRIORITY_BACKGROUND

    limits = Limits.from_settings(settings) if settings is not None else Limits()
    out = {"observed": 0, "stale": 0, "queued": 0}
    if not limits.enabled:
        return out
    with engine.begin() as connection:
        out["observed"] = observe_bindings(connection, project_id)
        if sweep:
            out["stale"] = mark_stale(connection, recheck_minutes=limits.recheck_minutes,
                                      project_id=project_id)
        for row in _due_rows(connection, project_id=project_id, limit=limit):
            if _in_flight(connection, row):
                continue       # stays due: queued again once the job in flight ends
            if _enqueue(connection, row, priority=JOB_PRIORITY_BACKGROUND):
                out["queued"] += 1
    return out


def _bindings_for(connection: Connection, project_id: str, repository: str) -> list[str]:
    ref = str(repository or "").strip()
    rows = connection.execute(text(
        "SELECT id FROM github_repository_bindings WHERE project_id = :p AND status = 'active' "
        "AND (:r = '' OR id = :r OR registry_repo_id = :r OR lower(full_name) = lower(:r)) ORDER BY full_name"),
        {"p": project_id, "r": ref}).scalars().all()
    return list(rows)


def request(connection: Connection, settings: Any, *, project_id: str, actor: str,
            repository: str = "", force: bool = False) -> dict[str, Any]:
    """Index now: the project's repositories (or the one named: owner/name or an id). One job
    per repository; a repository already being indexed is reported, not queued twice. `force`
    re-indexes a head that has not moved."""
    from ..jobs import JOB_PRIORITY_REQUESTED

    limits = Limits.from_settings(settings)
    if not limits.enabled:
        return {**status(connection, project_id), "queued": [],
                "message": "Indexing repositories is off on this install (BOSS_KG_REPO_INDEX=0)."}
    observe_bindings(connection, project_id)
    bindings = _bindings_for(connection, project_id, repository)
    if not bindings:
        said = f"No repository {repository!r} is registered in this project." if repository else \
            "No repository is registered in this project: register one first (register_repository)."
        return {**status(connection, project_id), "queued": [], "message": said}
    connection.execute(text(
        "UPDATE kg_repo_index SET due = true, reason = 'requested', force = (force OR :f), "
        "requested_by = :a, updated_at = now() WHERE project_id = :p AND binding_id = ANY(:bs)"),
        {"f": bool(force), "a": actor, "p": project_id, "bs": bindings})
    queued, running = [], []
    for row in _due_rows(connection, project_id=project_id, limit=len(bindings), bindings=bindings):
        if _in_flight(connection, row):
            running.append(row["repo"])
        elif _enqueue(connection, row, priority=JOB_PRIORITY_REQUESTED):
            queued.append(row["repo"])
    message = "; ".join(part for part in (
        f"Indexing {', '.join(queued)}" if queued else "",
        f"already indexing {', '.join(running)} (queued again when it ends)" if running else "") if part)
    return {**status(connection, project_id), "queued": queued, "in_flight": running, "message": message + "."}


def status(connection: Connection, project_id: str) -> dict[str, Any]:
    """Each registered repository's code graph: state, commit, counts, when, why not."""
    rows = connection.execute(text(
        """
        SELECT r.repo, r.branch, r.status, r.due, r.reason, r.job_id, r.indexed_sha, r.head_sha,
               r.files, r.parsed, r.nodes, r.edges, r.seconds, r.error, r.indexed_at,
               r.last_checked_at, r.last_run, b.id AS binding_id, b.full_name,
               COALESCE(b.default_branch, '') AS live_branch
        FROM github_repository_bindings b
        LEFT JOIN kg_repo_index r ON r.binding_id = b.id AND r.project_id = b.project_id
        WHERE b.project_id = :p AND b.status = 'active' ORDER BY b.full_name
        """), {"p": project_id}).mappings().all()
    # What the graph holds for each repository now (a build's own writes count too); edges are
    # the last run's count, kept on the row, so this read stays cheap on a large graph.
    code = {r[0]: int(r[1]) for r in connection.execute(text(
        "SELECT repo, count(*) FROM kg_nodes WHERE project_id = :p AND source = 'code_index' "
        "AND valid_to IS NULL AND repo IS NOT NULL GROUP BY repo"), {"p": project_id}).all()}
    out = []
    for r in rows:
        item = {k: r[k] for k in ("binding_id", "branch", "status", "due", "reason", "job_id",
                                  "indexed_sha", "head_sha", "seconds", "error")}
        item["repo"] = r["full_name"]
        for k in _NUMBERS:
            item[k] = r[k]
        if r["status"] is None:
            item.update(status="pending", due=True, reason="registered", branch=r["live_branch"])
        for k in ("indexed_at", "last_checked_at"):
            item[k] = r[k].isoformat() if r[k] is not None else None
        item["graph"] = {"nodes": code.get(item["repo"], 0), "edges": int(r["edges"] or 0)}
        item["summary"] = _summary(item)
        out.append(item)
    return {"project_id": project_id, "repositories": out}


def _summary(item: dict) -> str:
    """One line a person reads (the console's status line, the agent's answer)."""
    sha = (item.get("indexed_sha") or "")[:7]
    graph = item.get("graph") or {}
    if item["status"] == "indexed":
        return (f"{graph.get('nodes', item.get('nodes') or 0):,} nodes, {graph.get('edges', item.get('edges') or 0):,} "
                f"edges from {item.get('files') or 0:,} files at {sha or '?'} on {item.get('branch') or 'its branch'}"
                + (f", indexed {item['indexed_at'][:16].replace('T', ' ')} UTC" if item.get("indexed_at") else ""))
    if item["status"] in ("queued", "running"):
        return f"Indexing {item.get('branch') or 'its branch'} ({item['status']})."
    if item["status"] in ("failed", "too_large"):
        return f"Not indexed: {item.get('error') or item['status']}"
    return "Not indexed yet: queued at the next knowledge tick (within a minute)."


# --- running a job -------------------------------------------------------------------------------
class IndexRefused(Exception):
    """A run that cannot or must not index (too large, no such repository); not retried."""


def _git(args: list[str], *, cwd: Optional[Path] = None, env: Optional[dict] = None,
         timeout: int = 300) -> Any:
    from ..owned_process import boss_run

    return boss_run(["git", *args], cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                    timeout=timeout, env=env)


def _head(dest: Path) -> str:
    if not (dest / ".git").exists():
        return ""
    proc = _git(["rev-parse", "HEAD"], cwd=dest, timeout=30)
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""


def fetch_branch(url: str, branch: str, dest: Path, *, env: Optional[dict] = None,
                 timeout: int = 900) -> tuple[str, str]:
    """The branch's head, checked out (detached) in `dest`, one commit deep: (previous head,
    new head). An existing checkout fetches one commit instead of cloning again."""
    previous = _head(dest)
    if previous:
        fetched = _git(["fetch", "--quiet", "--depth", "1", url, branch], cwd=dest, env=env, timeout=timeout)
        if fetched.returncode == 0:
            done = _git(["checkout", "--quiet", "--force", "--detach", "FETCH_HEAD"], cwd=dest, timeout=300)
            if done.returncode == 0:
                _git(["clean", "-qfdx"], cwd=dest, timeout=300)
                return previous, _head(dest)
        shutil.rmtree(dest, ignore_errors=True)
        previous = ""
    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cloned = _git(["clone", "--quiet", "--depth", "1", "--branch", branch, url, str(dest)], env=env,
                  timeout=timeout)
    if cloned.returncode != 0:
        reason = (cloned.stderr or "").strip().splitlines()[-1:] or ["git clone failed"]
        raise RuntimeError(f"could not fetch {branch}: {reason[0][:300]}")
    return previous, _head(dest)


def _checkout(engine: Any, settings: Any, *, project_id: str, repo: str, branch: str,
              dest: Path, timeout: int) -> tuple[str, str]:
    """BOSS's own clone path (as the security scan's): the project's GitHub credential through
    identity.runs.repo_access, in git's environment only — never in the URL or .git/config."""
    from ...identity import runs as identity_runs
    from ..github_connections import github_token_for

    owner, _, name = repo.partition("/")

    def _token() -> str:
        with engine.begin() as c:
            return github_token_for(c, settings, project_id) or ""

    with identity_runs.repo_access(engine, host=getattr(settings, "target_repo_host", "github") or "github",
                                   owner=owner, repo=name, project_id=project_id, token=_token) as access:
        return fetch_branch(access.url, branch, dest, env=access.boss_environ(), timeout=timeout)


def _changed(dest: Path, old: str, new: str) -> Optional[tuple[list[str], list[str]]]:
    """(added or modified, deleted) between two commits of the checkout, or None when git cannot
    say (the old commit is gone)."""
    proc = _git(["diff", "--name-status", "--no-renames", old, new], cwd=dest, timeout=120)
    if proc.returncode != 0:
        return None
    changed, deleted = [], []
    for line in (proc.stdout or "").splitlines():
        code, _, path = line.partition("\t")
        (deleted if code.startswith("D") else changed).append(path)
    return changed, deleted


def plan(root: Path, limits: Limits) -> dict[str, Any]:
    """The tree against the bounds, before anything is parsed."""
    from .code_index import indexable, repository_paths

    paths = repository_paths(root)
    files = [p for p in paths if indexable(p)]
    total = 0
    for p in files:
        try:
            size = (root / p).stat().st_size
        except OSError:
            continue
        if size <= limits.max_file_bytes:
            total += size
    if len(files) > limits.max_files:
        raise IndexRefused(f"{len(files):,} source files is over the limit of {limits.max_files:,} "
                           "(BOSS_KG_INDEX_MAX_FILES)")
    if total > limits.max_total_bytes:
        raise IndexRefused(f"{total:,} bytes of source is over the limit of {limits.max_total_bytes:,} "
                           "(BOSS_KG_INDEX_MAX_TOTAL_BYTES)")
    return {"paths": paths, "source_files": len(files), "source_bytes": total}


def index_tree(engine: Any, *, project_id: str, repo: str, root: Path, limits: Limits,
               previous: str = "", head: str = "") -> dict[str, Any]:
    """Index one checkout into the project's graph, in ONE transaction (all or nothing).
    Incremental when `previous` is the commit last indexed and few files changed."""
    from .code_index import index_files, index_repository
    from .code_parse import language_for
    from .store import PostgresKnowledgeStore

    store = PostgresKnowledgeStore()
    with engine.begin() as connection:
        # The store's DDL in its own short transaction: inside the long one it would hold an
        # exclusive lock on the knowledge tables for the whole run, and every read would wait.
        # Bounded too: a long reader makes this run wait 15 s and retry, never queue every
        # other reader behind its lock.
        connection.execute(text("SET LOCAL lock_timeout = '15s'"))
        store.ensure_schema(connection)
    bounds = plan(root, limits)
    started = time.monotonic()
    diff = _changed(root, previous, head) if previous and head and previous != head else None
    with engine.begin() as connection:
        if diff is not None and len(diff[0]) + len(diff[1]) <= INCREMENTAL_MAX_FILES:
            from .code_index import indexable
            changed = []
            for path in diff[0]:
                target = root / path
                if not indexable(path) or not target.is_file() or target.stat().st_size > limits.max_file_bytes:
                    continue
                changed.append({"path": path, "language": _language(language_for(path)),
                                "source": target.read_text(errors="replace")})
            run = index_files(connection, store, project_id=project_id, files=changed, repo=repo) \
                if changed else {"indexed": 0}
            closed = store.close_unseen_code(connection, project_id=project_id, repo=repo,
                                             before=_now(), paths=diff[1]) if diff[1] else {"nodes": 0}
            run = {**run, "mode": "incremental", "changed": len(diff[0]), "deleted": len(diff[1]),
                   "closed_deleted": closed}
        else:
            run = index_repository(connection, store, project_id=project_id, root=root, repo=repo,
                                   max_file_bytes=limits.max_file_bytes, paths=bounds["paths"])
        counts = connection.execute(text(
            """
            SELECT (SELECT count(*) FROM kg_nodes WHERE project_id = :p AND repo = :r
                      AND source = 'code_index' AND valid_to IS NULL),
                   (SELECT count(*) FROM kg_nodes WHERE project_id = :p AND repo = :r
                      AND kind = 'file' AND source = 'code_index' AND valid_to IS NULL)
            """), {"p": project_id, "r": repo}).one()
        edges = connection.execute(text(
            "SELECT count(*) FROM kg_edges e WHERE e.project_id = :p AND e.valid_to IS NULL "
            "AND e.source = 'code_index' AND e.src_id IN (SELECT id FROM kg_nodes WHERE project_id = :p "
            "AND repo = :r AND valid_to IS NULL)"), {"p": project_id, "r": repo}).scalar()
    run.pop("unparsed", None)
    run.pop("too_large_paths", None)
    return {**{k: v for k, v in run.items() if isinstance(v, (int, float, str, dict))},
            "nodes": int(counts[0]), "files": int(counts[1]), "edges": int(edges or 0),
            "parsed": int(run.get("parsed", run.get("indexed", 0)) or 0),
            "source_files": bounds["source_files"], "seconds": round(time.monotonic() - started, 1)}


def _language(lang: Optional[str]) -> str:
    return "typescript" if lang == "tsx" else str(lang or "")


def _now():
    from ..core import now_utc
    return now_utc()


def _child(project_id: str, repo: str, root: Path, limits: Limits, previous: str, head: str,
           *, timeout: int) -> dict[str, Any]:
    """index_tree in a child process: memory goes back when it ends, the timeout kills it, and a
    kill rolls the transaction back."""
    from ..owned_process import boss_run

    args = [sys.executable, "-m", "boss_engine.services.knowledge.repo_index", "--project", project_id,
            "--repo", repo, "--root", str(root), "--previous", previous, "--head", head,
            "--limits", json.dumps(limits.__dict__)]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "GH_", "GITHUB_"))}
    try:
        proc = boss_run(args, capture_output=True, text=True, timeout=timeout, env=env)
    except Exception as error:  # noqa: BLE001 — TimeoutExpired, or the spawn refused
        if error.__class__.__name__ == "TimeoutExpired":
            raise IndexRefused(f"the index ran past {timeout:,} s (BOSS_KG_INDEX_TIMEOUT_SECONDS) "
                               "and was stopped; nothing it wrote was kept") from error
        raise
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.startswith("{")]
    if proc.returncode != 0 or not lines:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or [f"exit {proc.returncode}"]
        raise RuntimeError(f"the indexer failed: {tail[0][:300]}")
    out = json.loads(lines[-1])
    if out.get("refused"):
        raise IndexRefused(out["refused"])
    return out


def _work_dir(limits: Limits, binding_id: str) -> Path:
    base = Path(limits.work_dir) if limits.work_dir else Path(tempfile.gettempdir()) / "boss-kg-index"
    return base / binding_id


def _finish(engine: Any, row: dict, job_id: str, *, state: str, error: Optional[str] = None,
            head: str = "", run: Optional[dict] = None) -> None:
    from ..core import emit_event
    from ..receipts import ReceiptService

    run = run or {}
    with engine.begin() as c:
        sets = {"s": state, "e": error, "h": head or None, "j": job_id, "p": row["project_id"],
                "b": row["binding_id"], "run": json.dumps(run, default=str)}
        numbers = {k: run.get(k) for k in _NUMBERS}
        c.execute(text(
            """
            UPDATE kg_repo_index SET status = :s, error = :e, head_sha = COALESCE(:h, head_sha),
                   last_checked_at = now(), updated_at = now(), force = false,
                   last_run = CAST(:run AS JSONB),
                   indexed_sha = CASE WHEN :s = 'indexed' THEN COALESCE(:h, indexed_sha) ELSE indexed_sha END,
                   indexed_at = CASE WHEN :s = 'indexed' AND :changed THEN now() ELSE indexed_at END,
                   files = COALESCE(:files, files), parsed = COALESCE(:parsed, parsed),
                   nodes = COALESCE(:nodes, nodes), edges = COALESCE(:edges, edges),
                   seconds = COALESCE(:secs, seconds)
            WHERE project_id = :p AND binding_id = :b AND (job_id = :j OR job_id IS NULL)
            """), {**sets, **numbers, "secs": run.get("seconds"), "changed": run.get("mode") != "unchanged"})
        scope = c.execute(text(
            "SELECT w.org_id, p.workspace_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
            "WHERE p.id = :p"), {"p": row["project_id"]}).one()
        receipts = ReceiptService()
        payload = {"repo": row["repo"], "branch": row["branch"], "head": head, "status": state,
                   "error": error, **{k: run.get(k) for k in ("mode", "files", "parsed", "nodes", "edges", "seconds")}}
        receipt, _ = receipts.record_completed(
            c, scope[0], scope[1], row["project_id"], f"kg-index:{row['binding_id']}:{job_id}", EFFECT_KIND,
            {"repo": row["repo"], "branch": row["branch"]}, payload, provider_ref=head or None,
            created_by_job_id=job_id)
        if state != "indexed":
            receipts.mark_failed(c, receipt["id"], {"error": error or state})
        emit_event(c, row["project_id"], "knowledge.repository.indexed" if state == "indexed"
                   else "knowledge.repository.index_failed", "control_plane", "repository",
                   row["binding_id"], payload)


def execute_job(engine: Any, settings: Any, *, job_id: str, worker_id: str, lease_token: str,
                checkout: Optional[Callable[..., tuple[str, str]]] = None,
                inline: bool = False) -> dict[str, Any]:
    """The worker's leased `knowledge.repository.index` job, run here (the API holds the GitHub
    credential). `checkout` and `inline` are for tests: a local fetch, and the index in-process."""
    from ..jobs import JobLeaseService, LeaseNotFoundError

    limits = Limits.from_settings(settings)
    with engine.begin() as c:
        try:
            job = JobLeaseService(settings).require_active_lease(c, worker_id, job_id, lease_token)
        except LeaseNotFoundError as exc:
            return {"status": "failed", "retry": False, "error": f"lease: {exc}"}
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else json.loads(job.get("payload") or "{}")
        row = c.execute(text(
            "SELECT r.*, b.full_name, COALESCE(b.default_branch, r.branch) AS live_branch, b.status AS binding_status "
            "FROM kg_repo_index r JOIN github_repository_bindings b ON b.id = r.binding_id "
            "WHERE r.project_id = :p AND r.binding_id = :b"),
            {"p": job["project_id"], "b": payload.get("binding_id")}).mappings().first()
        if row is None or row["binding_status"] != "active":
            return {"status": "failed", "retry": False, "error": "the repository is no longer registered"}
        row = {**dict(row), "repo": row["full_name"], "branch": row["live_branch"] or "main"}
        c.execute(text("UPDATE kg_repo_index SET status = 'running', job_id = :j, repo = :r, branch = :br, "
                       "updated_at = now() WHERE project_id = :p AND binding_id = :b"),
                  {"j": job_id, "r": row["repo"], "br": row["branch"], "p": row["project_id"], "b": row["binding_id"]})
    dest = _work_dir(limits, row["binding_id"])
    head = ""
    try:
        previous, head = (checkout or _checkout_for(engine, settings))(
            project_id=row["project_id"], repo=row["repo"], branch=row["branch"], dest=dest,
            timeout=min(900, limits.timeout_seconds))
        last = row.get("indexed_sha") or ""
        if head and head == last and not (row.get("force") or payload.get("force")):
            run = {"mode": "unchanged", "files": row.get("files"), "parsed": 0, "nodes": row.get("nodes"),
                   "edges": row.get("edges"), "seconds": 0}
            _finish(engine, row, job_id, state="indexed", head=head, run=run)
            return {"status": "succeeded", "mode": "unchanged", "head": head}
        since = last if last and previous == last else ""
        if inline:
            run = index_tree(engine, project_id=row["project_id"], repo=row["repo"], root=dest,
                             limits=limits, previous=since, head=head)
        else:
            run = _child(row["project_id"], row["repo"], dest, limits, since, head,
                         timeout=limits.timeout_seconds)
    except IndexRefused as refused:
        _finish(engine, row, job_id, state="too_large" if "limit" in str(refused) else "failed",
                error=str(refused), head=head)
        return {"status": "failed", "retry": False, "error": str(refused)}
    except Exception as error:  # noqa: BLE001 — recorded on the row, the receipt and the event
        said = f"{error.__class__.__name__}: {str(error)[:300]}"
        _finish(engine, row, job_id, state="failed", error=said, head=head)
        return {"status": "failed", "retry": True, "error": said}
    _finish(engine, row, job_id, state="indexed", head=head, run=run)
    return {"status": "succeeded", "head": head, **{k: run.get(k) for k in ("mode", "files", "nodes", "edges", "seconds")}}


def _checkout_for(engine: Any, settings: Any) -> Callable[..., tuple[str, str]]:
    def run(**kwargs: Any) -> tuple[str, str]:
        return _checkout(engine, settings, **kwargs)
    return run


def main(argv: Optional[list[str]] = None) -> int:
    """The child process: index one checkout, print one JSON line."""
    import argparse

    from ...config import get_settings
    from ...db import get_engine

    parser = argparse.ArgumentParser()
    for name in ("--project", "--repo", "--root", "--previous", "--head", "--limits"):
        parser.add_argument(name, default="")
    args = parser.parse_args(argv)
    limits = Limits(**json.loads(args.limits or "{}"))
    engine = get_engine(get_settings())
    try:
        out = index_tree(engine, project_id=args.project, repo=args.repo, root=Path(args.root),
                         limits=limits, previous=args.previous, head=args.head)
    except IndexRefused as refused:
        out = {"refused": str(refused)}
    print(json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
