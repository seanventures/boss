"""Workspace snapshots across ephemeral sandboxes (BOSSAPP-826).

WHY. A governed build's work survives today only as commits pushed at stage
checkpoints (governed_code_task_service._checkpoint), and warm reuse is
same-host only (lineage_sandbox, /var/lib/boss/lineage-sandboxes). Once a build
runs OFF the box (the 824 SSH driver), every repair round lands on a fresh
remote checkout and re-installs every dependency.

WHAT. When BOSS_WORKSPACE_SNAPSHOTS=remote and the sandbox is remote:

  * at each stage checkpoint, `capture` tars the sandbox's UNCOMMITTED tree
    (sandbox.diff(): changed + new files, .gitignore honoured, build artefacts
    and binaries out — the same set the checkpoint pushes) and its DEPENDENCY
    DIRS (node_modules, .venv, venv — the expensive part) into BOSS's object
    store, via the 824 sandbox `sync_out` seam, and indexes it in
    `workspace_snapshots` (kind='full', generation 1, 2, … per root run);
  * dependency dirs are stored ONCE per lockfile state as a kind='seed'
    snapshot keyed by a hash of the repository's top-level lockfiles
    (package-lock.json, pnpm-lock.yaml, yarn.lock, requirements*.txt, …). A full
    snapshot then references its seed instead of carrying node_modules again —
    four checkpoints in a build upload the dependencies once;
  * when the governed path provisions a sandbox for a repair of the same root
    run, `restore` brings back the latest full snapshot (sync_in) before the
    stage runs; a FRESH run of the same repo restores the seed matching its
    lockfiles, so it warm-starts dependencies;
  * `reap_expired`, registered on resource_reaper.tick, deletes rows past their
    TTL together with their objects.

SNAPSHOT FORMAT (one gzip'd tar per object):
    manifest.json   {"format": "boss.workspace-snapshot/1", "kind", "run_id",
                     "root_run_id", "generation", "label", "seed_key",
                     "deps": [<dependency dir, repo-relative>],
                     "tree": [<repo-relative file>], "created_at"}
    tree/<path>     the uncommitted files (full only)
    deps/<dir>/...  the dependency dirs, symlinks and +x kept (seed; or a full
                    snapshot of a repo with no lockfile to key a seed on)

OBJECT KEYS:
    workspace-snapshots/runs/<root_run_id>/g<generation:06d>-<run_id>.tar.gz
    workspace-snapshots/seeds/<project_id>/<owner__repo>/<seed_key>.tar.gz

WHAT A RESTORE NEVER DOES: overwrite a file the fresh clone already has. The
clone is of the lineage's work branch, which the checkpoint push (or an
operator) may have moved on since the snapshot; git's copy wins. A restore only
adds the files the clone lacks, plus the dependency dirs.

With BOSS_WORKSPACE_SNAPSHOTS=off (the default) nothing here runs: the governed
service guards both hooks with `enabled_for`, which reads two settings and
touches no database, store or sandbox.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import shlex
import tarfile
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Optional

from sqlalchemy import text

from .core import make_id
from .object_store import (
    ObjectNotFound,
    ObjectStore,
    ObjectStoreError,
    key_part,
    object_store_from_settings,
    sha256_file,
)

_log = logging.getLogger(__name__)

MODES = ("off", "remote")
FORMAT = "boss.workspace-snapshot/1"
KEY_PREFIX = "workspace-snapshots"
#: Provider kinds whose checkout lives on this box: lineage_sandbox's warm reuse
#: already covers them ('microvm' keeps a local checkout as the git carrier).
LOCAL_KINDS = ("local", "subprocess", "local_subprocess", "microvm")
#: The expensive, re-installable dirs. Searched to this depth so a monorepo's
#: apps/web/node_modules is caught; a match is pruned (no node_modules inside
#: node_modules).
DEPENDENCY_DIR_NAMES = ("node_modules", ".venv", "venv")
DEPENDENCY_SEARCH_DEPTH = 3
#: Top-level lockfiles that decide what a dependency install produces.
LOCKFILE_RE = re.compile(
    r"^(package-lock\.json|npm-shrinkwrap\.json|pnpm-lock\.yaml|yarn\.lock|bun\.lockb"
    r"|poetry\.lock|uv\.lock|Pipfile\.lock|requirements[A-Za-z0-9._-]*\.txt)$")
#: How many paths one `git ls-files` presence check carries.
_PRESENCE_CHUNK = 200


@dataclass
class SnapshotContext:
    """Who a snapshot belongs to — built once at the provision site."""

    engine: Any
    settings: Any
    store: ObjectStore
    run_id: str
    root_run_id: str
    project_id: Optional[str]
    repo: str  # owner/repo


# ── when ─────────────────────────────────────────────────────────────────────


def mode(settings: Any) -> str:
    value = str(getattr(settings, "workspace_snapshots", "off") or "off").strip().lower()
    return value if value in MODES else "off"


def is_remote(provider: Any, sandbox: Any) -> bool:
    """A sandbox counts as remote when its provider is not a local kind AND it
    speaks the 824 sync seam (sync_in / sync_out)."""
    kind = str(getattr(provider, "kind", "local") or "local").lower()
    return (kind not in LOCAL_KINDS
            and callable(getattr(sandbox, "sync_out", None))
            and callable(getattr(sandbox, "sync_in", None)))


def enabled_for(settings: Any, provider: Any, sandbox: Any) -> bool:
    """The one guard both governed hooks use. Off -> False before anything else
    is looked at (the byte-identical default)."""
    if mode(settings) != "remote":
        return False
    if not is_remote(provider, sandbox):
        return False
    if not (getattr(settings, "s3_bucket", None) or "").strip():
        _log.warning("BOSS_WORKSPACE_SNAPSHOTS=remote but S3_BUCKET is unset; "
                     "workspace snapshots are skipped")
        return False
    return True


def context_for(engine: Any, settings: Any, *, run_id: str, root_run_id: str,
                project_id: Optional[str], owner: str, repo: str) -> Optional[SnapshotContext]:
    store = object_store_from_settings(settings)
    if store is None:
        return None
    return SnapshotContext(engine=engine, settings=settings, store=store, run_id=run_id,
                           root_run_id=root_run_id or run_id, project_id=project_id,
                           repo=f"{owner}/{repo}")


# ── reading the remote tree (through the Sandbox protocol only) ──────────────


def _run(sandbox: Any, command: str, *, timeout: int = 120,
         env: Optional[dict] = None) -> Optional[str]:
    try:
        result = sandbox.run(command, timeout=timeout, **({"env": env} if env else {}))
    except Exception:  # noqa: BLE001 — a probe that cannot run answers "nothing"
        _log.debug("workspace snapshot probe %r failed", command, exc_info=True)
        return None
    if getattr(result, "exit_code", 1) != 0:
        return None
    return getattr(result, "stdout", "") or ""


def discover_dependency_dirs(sandbox: Any) -> list[str]:
    """Repo-relative dependency dirs present in the sandbox, outermost only."""
    found: set[str] = set()
    for name in DEPENDENCY_DIR_NAMES:
        out = _run(sandbox, f"find . -maxdepth {DEPENDENCY_SEARCH_DEPTH} -type d "
                            f"-name {name} -prune")
        for line in (out or "").splitlines():
            rel = line.strip()
            while rel.startswith("./"):
                rel = rel[2:]
            parts = PurePosixPath(rel).parts if rel else ()
            if not parts or ".git" in parts or ".." in parts:
                continue
            if any(p in DEPENDENCY_DIR_NAMES for p in parts[:-1]):
                continue  # inside another dependency dir
            found.add(rel)
    return sorted(found)


def git_blob_sha(data: bytes) -> str:
    """`git hash-object` of these bytes — the content address the remote
    reports for a lockfile."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324 — git's id


def seed_key_from(pairs: Iterable[tuple[str, str]]) -> Optional[str]:
    """The seed cache key: sha256 over the sorted (lockfile, blob sha) pairs.
    None when the repository has no lockfile (nothing to key a seed on)."""
    items = sorted((str(n), str(s)) for n, s in pairs)
    if not items:
        return None
    body = "".join(f"{name}\0{sha}\n" for name, sha in items)
    return hashlib.sha256(f"{FORMAT}\n{body}".encode()).hexdigest()


def lock_key(sandbox: Any) -> Optional[str]:
    """The seed key of the sandbox's CURRENT top-level lockfiles (working tree,
    so a dependency the agent just added changes the key)."""
    listing = _run(sandbox, "ls -1A")
    if listing is None:
        return None
    names = sorted(n.strip() for n in listing.splitlines() if LOCKFILE_RE.match(n.strip()))
    if not names:
        return None
    out = _run(sandbox, "git hash-object -- " + " ".join(shlex.quote(n) for n in names))
    shas = (out or "").split()
    if len(shas) != len(names):
        return None
    return seed_key_from(zip(names, shas))


def _present_in_clone(sandbox: Any, paths: list[str]) -> set[str]:
    """Which of these repo-relative paths the sandbox already has (tracked, or
    untracked and not ignored). Literal pathspecs: no globbing."""
    present: set[str] = set()
    for start in range(0, len(paths), _PRESENCE_CHUNK):
        chunk = paths[start:start + _PRESENCE_CHUNK]
        out = _run(sandbox,
                   "git ls-files --cached --others --exclude-standard -- "
                   + " ".join(shlex.quote(p) for p in chunk),
                   env={"GIT_LITERAL_PATHSPECS": "1"})
        if out is None:
            return set(paths)  # cannot tell -> treat all as present: never overwrite
        present.update(line.strip() for line in out.splitlines() if line.strip())
    return present


# ── archive format ───────────────────────────────────────────────────────────


def _safe_rel(path: str) -> Optional[str]:
    parts = PurePosixPath(path).parts
    if not parts or path.startswith("/") or any(p in ("", ".", "..") for p in parts):
        return None
    return "/".join(parts)


def _sync_out(sandbox: Any, remote_rel: str, local: Path) -> None:
    """The 824 seam, asking it to keep links and modes when it can."""
    try:
        sandbox.sync_out(remote_rel, local, preserve_links=True)
    except TypeError:
        sandbox.sync_out(remote_rel, local)


def build_archive(path: Path, manifest: dict, staging: Path) -> None:
    """manifest.json + staging/tree + staging/deps -> one tar.gz at path."""
    body = json.dumps(manifest, sort_keys=True, indent=1).encode()
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(body)
        info.mtime = int(time.time())
        archive.addfile(info, io.BytesIO(body))
        for top in ("tree", "deps"):
            if (staging / top).is_dir():
                archive.add(staging / top, arcname=top)  # symlinks stay symlinks


def extract_archive(path: Path, destination: Path) -> dict:
    """Unpack a snapshot; returns its manifest. Members outside tree/ deps/ or
    escaping the destination are skipped; nothing is written through a link."""
    from .ssh_environment import _extract_preserving_links

    destination.mkdir(parents=True, exist_ok=True)
    manifest: dict = {}
    with tarfile.open(path, "r:gz") as archive:
        member = archive.getmember("manifest.json")
        handle = archive.extractfile(member)
        manifest = json.loads(handle.read().decode()) if handle is not None else {}
    if manifest.get("format") != FORMAT:
        raise ObjectStoreError(f"not a workspace snapshot (format {manifest.get('format')!r})")
    with tarfile.open(path, "r:gz") as archive:
        _extract_preserving_links(_only_payload(archive), destination)
    return manifest


class _only_payload:
    """Iterate a TarFile's tree/ and deps/ members only (duck-types TarFile for
    the extractor)."""

    def __init__(self, archive: tarfile.TarFile):
        self._archive = archive

    def __iter__(self):
        for member in self._archive:
            if member.name.split("/", 1)[0] in ("tree", "deps"):
                yield member

    def extractfile(self, member):
        return self._archive.extractfile(member)


# ── database ─────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _jsonb(connection) -> str:
    try:
        name = connection.engine.dialect.name
    except AttributeError:
        name = "sqlite"
    return ":m" if name == "sqlite" else "CAST(:m AS JSONB)"


def _manifest_of(row: dict) -> dict:
    value = row.get("manifest")
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        return {}


def _alive(row: dict, now: datetime) -> bool:
    expires = _parse_time(row.get("expires_at"))
    return expires is None or expires > now


def find_seed(connection, *, project_id: Optional[str], repo: str,
              seed_key: str) -> Optional[dict]:
    # Seeds never cross a project boundary (a dependency dir can carry what a
    # postinstall wrote): the project is part of the key.
    scope = "project_id = :p" if project_id is not None else "project_id IS NULL"
    rows = connection.execute(
        text("SELECT * FROM workspace_snapshots WHERE kind = 'seed' AND repo = :r "
             f"AND seed_key = :k AND {scope}"),
        {"r": repo, "k": seed_key, **({"p": project_id} if project_id is not None else {})},
    ).mappings().all()
    now = _now()
    for row in rows:
        if _alive(dict(row), now):
            return dict(row)
    return None


def latest_full(connection, *, root_run_id: str, exclude_run_id: Optional[str] = None,
                ) -> Optional[dict]:
    rows = connection.execute(
        text("SELECT * FROM workspace_snapshots WHERE kind = 'full' AND root_run_id = :root "
             "ORDER BY generation DESC"),
        {"root": root_run_id},
    ).mappings().all()
    now = _now()
    for row in rows:
        row = dict(row)
        if exclude_run_id and row.get("run_id") == exclude_run_id:
            continue
        if _alive(row, now):
            return row
    return None


def _next_generation(connection, root_run_id: str) -> int:
    value = connection.execute(
        text("SELECT MAX(generation) FROM workspace_snapshots "
             "WHERE kind = 'full' AND root_run_id = :root"), {"root": root_run_id},
    ).scalar()
    return int(value or 0) + 1


def _insert(connection, row: dict) -> bool:
    result = connection.execute(
        text("INSERT INTO workspace_snapshots (id, run_id, root_run_id, project_id, repo, "
             "kind, generation, label, seed_key, object_key, digest, bytes, manifest, "
             "expires_at, last_used_at, created_at) VALUES (:id, :run_id, :root, :p, :repo, "
             f":kind, :gen, :label, :seed, :key, :digest, :bytes, {_jsonb(connection)}, "
             ":exp, :now, :now) ON CONFLICT DO NOTHING"),
        {"id": row["id"], "run_id": row["run_id"], "root": row.get("root_run_id"),
         "p": row.get("project_id"), "repo": row.get("repo") or "", "kind": row["kind"],
         "gen": int(row.get("generation") or 0), "label": row.get("label"),
         "seed": row.get("seed_key"), "key": row["object_key"], "digest": row["digest"],
         "bytes": int(row.get("bytes") or 0), "m": json.dumps(row.get("manifest") or {}),
         "exp": row["expires_at"], "now": _now().isoformat()},
    )
    return bool(getattr(result, "rowcount", 1))


def _touch_seed(connection, seed_id: str, ttl_hours: int) -> None:
    now = _now()
    connection.execute(
        text("UPDATE workspace_snapshots SET last_used_at = :now, expires_at = :exp "
             "WHERE id = :id"),
        {"id": seed_id, "now": now.isoformat(),
         "exp": (now + timedelta(hours=ttl_hours)).isoformat()},
    )


def _ttl_hours(settings: Any, kind: str) -> int:
    if kind == "seed":
        return int(getattr(settings, "workspace_seed_ttl_hours", 24 * 7) or 24 * 7)
    return int(getattr(settings, "workspace_snapshot_ttl_hours", 72) or 72)


# ── keys ─────────────────────────────────────────────────────────────────────


def full_key(root_run_id: str, generation: int, run_id: str) -> str:
    return (f"{KEY_PREFIX}/runs/{key_part(root_run_id)}/"
            f"g{generation:06d}-{key_part(run_id)}.tar.gz")


def seed_object_key(project_id: Optional[str], repo: str, seed_key: str) -> str:
    return (f"{KEY_PREFIX}/seeds/{key_part(project_id or 'org')}/"
            f"{key_part(repo.replace('/', '__'))}/{key_part(seed_key)}.tar.gz")


# ── capture ──────────────────────────────────────────────────────────────────


def _upload(ctx: SnapshotContext, archive: Path, key: str) -> tuple[str, int]:
    digest = sha256_file(archive)
    ctx.store.put_file(key, archive)
    return digest, archive.stat().st_size


def _write_seed(ctx: SnapshotContext, sandbox: Any, seed_key: str, deps: list[str],
                work: Path, label: str) -> Optional[dict]:
    staging = work / "seed"
    for rel in deps:
        _sync_out(sandbox, rel, staging / "deps" / rel)
    manifest = {"format": FORMAT, "kind": "seed", "run_id": ctx.run_id,
                "root_run_id": ctx.root_run_id, "generation": 0, "label": label,
                "seed_key": seed_key, "deps": deps, "tree": [],
                "created_at": _now().isoformat()}
    archive = work / "seed.tar.gz"
    build_archive(archive, manifest, staging)
    key = seed_object_key(ctx.project_id, ctx.repo, seed_key)
    digest, size = _upload(ctx, archive, key)
    row = {"id": make_id("wss"), "run_id": ctx.run_id, "root_run_id": ctx.root_run_id,
           "project_id": ctx.project_id, "repo": ctx.repo, "kind": "seed", "generation": 0,
           "label": label, "seed_key": seed_key, "object_key": key, "digest": digest,
           "bytes": size, "manifest": manifest,
           "expires_at": (_now() + timedelta(hours=_ttl_hours(ctx.settings, "seed"))
                          ).isoformat()}
    with ctx.engine.begin() as connection:
        if not _insert(connection, row):
            # Another run wrote the same seed first (same lockfiles): theirs
            # stands. Our upload landed on the same key with equivalent content.
            return find_seed(connection, project_id=ctx.project_id, repo=ctx.repo,
                             seed_key=seed_key)
    return row


def capture(ctx: SnapshotContext, sandbox: Any, label: str) -> dict:
    """Snapshot a remote sandbox at a stage boundary. Returns a summary; raises
    on failure (the caller's best-effort wrapper decides)."""
    tree = list(sandbox.diff() or [])
    deps = discover_dependency_dirs(sandbox)
    seed_key = lock_key(sandbox) if deps else None
    summary: dict = {"label": label, "tree_files": 0, "deps": deps, "seed_key": seed_key,
                     "seed": None}
    with tempfile.TemporaryDirectory(prefix="boss-snapshot-") as tmp:
        work = Path(tmp)
        staging = work / "full"
        embed_deps = bool(deps) and not seed_key
        if seed_key:
            with ctx.engine.begin() as connection:
                seed = find_seed(connection, project_id=ctx.project_id, repo=ctx.repo,
                                 seed_key=seed_key)
                if seed is not None:
                    _touch_seed(connection, seed["id"], _ttl_hours(ctx.settings, "seed"))
            if seed is None:
                seed = _write_seed(ctx, sandbox, seed_key, deps, work, label)
                summary["seed"] = "written"
            else:
                summary["seed"] = "reused"
        if embed_deps:
            for rel in deps:
                _sync_out(sandbox, rel, staging / "deps" / rel)
        written: list[str] = []
        for change in tree:
            rel = _safe_rel(str(getattr(change, "path", "") or ""))
            content = getattr(change, "content", None)
            if rel is None or content is None:
                continue
            target = staging / "tree" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content if isinstance(content, bytes)
                               else str(content).encode("utf-8"))
            written.append(rel)
        with ctx.engine.begin() as connection:
            generation = _next_generation(connection, ctx.root_run_id)
        manifest = {"format": FORMAT, "kind": "full", "run_id": ctx.run_id,
                    "root_run_id": ctx.root_run_id, "generation": generation,
                    "label": label, "seed_key": seed_key,
                    "deps": deps if embed_deps else [], "tree": sorted(written),
                    "created_at": _now().isoformat()}
        archive = work / "full.tar.gz"
        build_archive(archive, manifest, staging)
        key = full_key(ctx.root_run_id, generation, ctx.run_id)
        digest, size = _upload(ctx, archive, key)
        row = {"id": make_id("wss"), "run_id": ctx.run_id, "root_run_id": ctx.root_run_id,
               "project_id": ctx.project_id, "repo": ctx.repo, "kind": "full",
               "generation": generation, "label": label, "seed_key": seed_key,
               "object_key": key, "digest": digest, "bytes": size, "manifest": manifest,
               "expires_at": (_now() + timedelta(hours=_ttl_hours(ctx.settings, "full"))
                              ).isoformat()}
        with ctx.engine.begin() as connection:
            if not _insert(connection, row):
                raise ObjectStoreError(
                    f"snapshot generation {generation} of {ctx.root_run_id} already exists")
    summary.update({"tree_files": len(written), "generation": generation, "object_key": key,
                    "digest": digest, "bytes": size})
    return summary


# ── restore ──────────────────────────────────────────────────────────────────


def _fetch(ctx: SnapshotContext, row: dict, work: Path, name: str) -> Path:
    archive = work / f"{name}.tar.gz"
    ctx.store.get_file(str(row["object_key"]), archive)
    if sha256_file(archive) != row["digest"]:
        raise ObjectStoreError(f"snapshot {row['object_key']} failed its digest check")
    return archive


def _apply_deps(sandbox: Any, unpacked: Path, deps: list[str]) -> list[str]:
    applied = []
    for dep in deps:
        rel = _safe_rel(dep)
        if rel and (unpacked / "deps" / rel).is_dir():
            sandbox.sync_in(unpacked / "deps" / rel, rel)
            applied.append(rel)
    return applied


def restore(ctx: SnapshotContext, sandbox: Any, *, is_repair: bool) -> dict:
    """Bring a provisioned remote sandbox up to the latest snapshot: a repair
    gets its lineage's latest full snapshot (files the clone lacks + deps); any
    run whose deps are still missing gets the seed for its lockfiles."""
    summary: dict = {"full": None, "seed": None, "tree_files": 0, "deps": []}
    with tempfile.TemporaryDirectory(prefix="boss-restore-") as tmp:
        work = Path(tmp)
        full = None
        if is_repair:
            with ctx.engine.begin() as connection:
                full = latest_full(connection, root_run_id=ctx.root_run_id,
                                   exclude_run_id=ctx.run_id)
        if full is not None:
            unpacked = work / "full"
            manifest = extract_archive(_fetch(ctx, full, work, "full"), unpacked)
            tree = [p for p in (_safe_rel(x) for x in manifest.get("tree") or []) if p]
            present = _present_in_clone(sandbox, tree) if tree else set()
            missing = [p for p in tree if p not in present]
            if missing:
                only = work / "tree-missing"
                for rel in missing:
                    source = unpacked / "tree" / rel
                    if source.is_file():
                        (only / rel).parent.mkdir(parents=True, exist_ok=True)
                        (only / rel).write_bytes(source.read_bytes())
                if only.is_dir():
                    sandbox.sync_in(only, "")
            summary.update({"full": full["object_key"], "generation": full.get("generation"),
                            "tree_files": len(missing)})
            summary["deps"] = _apply_deps(sandbox, unpacked, list(manifest.get("deps") or []))
        if not summary["deps"]:
            key = lock_key(sandbox)
            seed = None
            if key:
                with ctx.engine.begin() as connection:
                    seed = find_seed(connection, project_id=ctx.project_id, repo=ctx.repo,
                                     seed_key=key)
                    if seed is not None:
                        _touch_seed(connection, seed["id"], _ttl_hours(ctx.settings, "seed"))
            summary["seed_key"] = key
            if seed is not None:
                unpacked = work / "seed"
                manifest = extract_archive(_fetch(ctx, seed, work, "seed"), unpacked)
                summary["deps"] = _apply_deps(sandbox, unpacked,
                                              list(manifest.get("deps") or []))
                summary["seed"] = seed["object_key"]
    return summary


# ── best-effort entry points for the governed service ────────────────────────


def _emit(ctx: SnapshotContext, event: str, payload: dict) -> None:
    try:
        from .core import emit_event

        with ctx.engine.begin() as connection:
            emit_event(connection, ctx.project_id, event, "boss", "code_task_run",
                       ctx.run_id, payload)
    except Exception:  # noqa: BLE001 — an event is evidence, never a failure
        _log.debug("workspace snapshot event %s not recorded", event, exc_info=True)


def capture_best_effort(ctx: Optional[SnapshotContext], sandbox: Any, label: str) -> Optional[dict]:
    """The _checkpoint hook: never raises, never fails the run."""
    if ctx is None or sandbox is None:
        return None
    try:
        summary = capture(ctx, sandbox, label)
    except Exception as exc:  # noqa: BLE001
        _log.warning("workspace snapshot after %s failed", label, exc_info=True)
        _emit(ctx, "code_task.workspace_snapshot_failed",
              {"after": label, "error": f"{type(exc).__name__}: {exc}"[:200]})
        return None
    _emit(ctx, "code_task.workspace_snapshot",
          {k: summary.get(k) for k in ("label", "generation", "tree_files", "deps",
                                       "seed", "bytes")})
    return summary


def restore_best_effort(ctx: Optional[SnapshotContext], sandbox: Any, *,
                        is_repair: bool) -> Optional[dict]:
    """The provision-site hook: never raises — a missed restore costs a
    re-install, never the run."""
    if ctx is None or sandbox is None:
        return None
    try:
        summary = restore(ctx, sandbox, is_repair=is_repair)
    except Exception as exc:  # noqa: BLE001
        _log.warning("workspace snapshot restore failed", exc_info=True)
        _emit(ctx, "code_task.workspace_restore_failed",
              {"error": f"{type(exc).__name__}: {exc}"[:200]})
        return None
    if summary.get("full") or summary.get("seed"):
        _emit(ctx, "code_task.workspace_restored",
              {k: summary.get(k) for k in ("full", "generation", "seed", "tree_files",
                                           "deps")})
    return summary


# ── reaper ───────────────────────────────────────────────────────────────────


def reap_expired(engine: Any, settings: Any = None, *,
                 store: Optional[ObjectStore] = None) -> dict:
    """Delete snapshots past expires_at, object first, then the row. Registered
    on resource_reaper.tick (the 824 reap_leases pattern). With no store
    configured nothing is touched — not even the table — since an object it
    cannot delete must keep its row. Tolerates a pre-826 schema."""
    if store is None:
        if settings is None:
            from ..config import get_settings

            settings = get_settings()
        store = object_store_from_settings(settings)
    if store is None:
        return {"expired": 0, "skipped": "no_store"}
    try:
        with engine.begin() as connection:
            rows = connection.execute(
                text("SELECT id, object_key, expires_at FROM workspace_snapshots "
                     "WHERE expires_at IS NOT NULL")
            ).mappings().all()
    except Exception as exc:  # noqa: BLE001 — pre-826 schema: nothing to reap
        return {"expired": 0, "skipped": f"{type(exc).__name__}"[:80]}
    now = _now()
    expired = failed = 0
    for row in rows:
        expires = _parse_time(row["expires_at"])
        if expires is None or expires > now:
            continue
        try:
            store.delete(str(row["object_key"]))
        except ObjectNotFound:
            pass
        except Exception:  # noqa: BLE001 — keep the row; the next tick retries
            failed += 1
            _log.warning("workspace snapshot %s: object delete failed", row["id"],
                         exc_info=True)
            continue
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM workspace_snapshots WHERE id = :i"),
                               {"i": row["id"]})
        expired += 1
    if expired:
        _log.info("workspace_snapshots: reaped %s expired snapshot(s)", expired)
    return {"expired": expired, "failed": failed}
