"""AN APP'S PREVIEW DATA OUTLIVES THE PREVIEW (recipe-box, 2026-11-18).

A preview is a fresh checkout in a directory named after the preview id, so whatever the app
saved there (its SQLite file, its data folder) went when the preview did: stopped by BOSS after
20 idle minutes, restarted, and the operator's recipes were gone. The data belongs to the APP,
not to one preview of it, so it is kept per app (project + repository) in BOSS's own store,
outside every preview tree:

* `mark_restored` — at launch, before the app runs: the app's kept data is copied into the new
  tree, and what the tree held before that is noted (its baseline);
* `capture` — when the preview ends (stopped, idle, expired, swept, or relaunched after a
  restart), ONLY AFTER ITS UID IS STOPPED (preview_service._end_and_keep_data): what the app
  WROTE (SQLite files anywhere, and files under a data folder) that differs from the baseline
  replaces the app's kept data;
* `reset` — the operator's "fresh data": the app's kept data is deleted.

THE TREE IS THE APP'S, SO IT IS READ AS UNTRUSTED (review of #223, B2). BOSS reads it with
more rights than the app has, so a symlink swapped in at the right moment would have copied a
BOSS file into the app's kept data, and the next restore would have handed it to the app. So:
the app's uid is stopped first (an orphaned lease too, found from its run directory), every file
is opened through directory fds with O_NOFOLLOW (workspace_perms._parent_fd) and kept only when
fstat says it is a regular file with one link, owned by the preview's uid or BOSS — never a
FIFO, a device, a hard link to something else; a file only the app's uid can read is read AS that
uid. Restored files are written as the preview's uid (0640, group boss) in the sandbox, and 0600
by BOSS without one — never readable by anyone else.

What counts as data is what an app writes, not what the repository ships: a file under a data
folder that the checkout already had unchanged is the repo's, and stays the repo's. A capture
that finds nothing never replaces kept data (an app that failed to start has written nothing),
and a capture over the size limit is refused and logged, never truncated.

The app's Postgres (build_services, a preview's throwaway database) is not covered here.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: Folders an app's data lives in by convention (Flask's instance/, Rails' storage/, data/).
DATA_DIRS = frozenset({"data", ".data", "storage", "uploads", "instance", "db"})
#: Never data: dependencies, build output, VCS, caches.
SKIP_DIRS = frozenset({"node_modules", ".git", ".next", ".nuxt", ".svelte-kit", "dist", "build",
                       ".npm-cache", ".cache", "__pycache__", ".venv", "venv", "target",
                       ".gradle", ".turbo", "coverage", ".pytest_cache", ".npm", ".config",
                       ".local"})
SQLITE_MAGIC = b"SQLite format 3\x00"
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
_TREES = "_trees"


def data_root() -> Path:
    """BOSS's store: BOSS_PREVIEW_DATA_DIR, else under the artifacts volume (it survives a
    container recreate), else beside the previews."""
    explicit = os.environ.get("BOSS_PREVIEW_DATA_DIR")
    if explicit:
        return Path(explicit)
    artifacts = Path(os.environ.get("BOSS_ARTIFACTS_DIR") or "/var/lib/boss/artifacts")
    if artifacts.is_dir() and os.access(artifacts, os.W_OK):
        return artifacts / "preview-data"
    from .preview_service import PREVIEW_ROOT

    return PREVIEW_ROOT / "_data"


def max_bytes() -> int:
    try:
        return int(float(os.environ.get("BOSS_PREVIEW_DATA_MAX_MB") or 256) * 1024 * 1024)
    except ValueError:
        return 256 * 1024 * 1024


def app_key(project_id: Optional[str], repo: Optional[str]) -> str:
    """One app: its project and repository. Every branch and every preview of it shares it."""
    project = str(project_id or "").strip()
    repository = str(repo or "").strip()
    if not project or not repository:
        return ""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", repository.rsplit("/", 1)[-1])[:40].strip(".-") or "app"
    digest = hashlib.sha256(f"{project}\x00{repository}".encode()).hexdigest()[:16]
    return f"{slug}-{digest}"


def _store(key: str) -> Path:
    return data_root() / key


def _tree_note(preview_id: str) -> Path:
    return data_root() / _TREES / f"{re.sub(r'[^A-Za-z0-9_-]', '_', preview_id)}.json"


def _trusted_root(tree: Path) -> Optional[str]:
    """Where a no-follow walk of `tree` may start: BOSS's sandbox root, or BOSS's own preview
    directory when the tree lies under it; None (start at "/") otherwise."""
    from . import workspace_perms
    from .preview_service import PREVIEW_ROOT

    found = workspace_perms.trusted_root_for(tree)
    if found:
        return found
    target = os.path.abspath(str(tree))
    for spelling in {os.path.abspath(str(PREVIEW_ROOT)), os.path.realpath(str(PREVIEW_ROOT))}:
        if target.startswith(spelling.rstrip("/") + "/"):
            return str(PREVIEW_ROOT)
    return None


def in_sandbox(tree: Path) -> bool:
    from . import workspace_perms

    return workspace_perms.trusted_root_for(tree) is not None


def _safe_rel(rel: str) -> bool:
    return bool(rel) and not os.path.isabs(rel) and ".." not in Path(rel).parts


def _open_regular(tree: Path, rel: str, *, owners: Optional[set[int]] = None) -> Optional[int]:
    """An fd for `tree/rel`, or None: every directory on the way opened with O_NOFOLLOW from a
    trusted root, the file itself O_NOFOLLOW|O_NONBLOCK (a FIFO never blocks), and kept only when
    fstat says regular, one link, and (with `owners`) owned by one of them. Raises
    PermissionError when BOSS may not read it (the caller may read it as the app's uid)."""
    if not _safe_rel(rel):
        return None
    from . import workspace_perms

    got = workspace_perms._parent_fd(tree, rel, trusted=_trusted_root(tree))  # noqa: SLF001
    if got is None:
        return None
    dfd, name = got
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                     | getattr(os, "O_CLOEXEC", 0), dir_fd=dfd)
    except PermissionError:
        raise
    except OSError:
        return None
    finally:
        os.close(dfd)
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1
                or (owners is not None and st.st_uid not in owners)):
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        return None
    return fd


class _OverLimit(Exception):
    """A file larger than what may still be kept."""


def _read_fd(fd: int, limit: int) -> bytes:
    """Up to `limit` bytes from `fd` (closed after); _OverLimit when there were more."""
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > limit:
                raise _OverLimit()
            chunks.append(chunk)
    finally:
        os.close(fd)


def _run_as(uid: int, script: str, *args: str, data: Optional[bytes] = None,
            timeout: float = 120) -> Optional[bytes]:
    """`script` run by /bin/sh AS `uid` (boss-sandbox-exec): stdout, or None when it failed."""
    from . import owned_process, sandbox_identity

    helper = sandbox_identity.helper_path()
    if helper is None:
        return None
    try:
        proc = owned_process.run([helper, "run", str(uid), "--", "/bin/sh", "-c", script, "sh", *args],
                                 input=data, capture_output=True, timeout=timeout, sandboxed=False,
                                 cwd="/")
    except Exception:  # noqa: BLE001 — a timeout or a missing helper reads as "not done"
        return None
    return proc.stdout if proc.returncode == 0 else None


#: As the app's uid, read one regular file that is not a link (its processes are stopped, so
#: nothing swaps it between the test and the read). The uid reads only what it may read anyway.
_READ_AS_UID = 'test -f "$1" && ! test -h "$1" && exec head -c "$2" -- "$1"'
#: As the app's uid, write stdin to one file in its tree, 0640 (group boss: BOSS reads it back).
_WRITE_AS_UID = ('umask 027; d=$(dirname -- "$1"); mkdir -p -- "$d" && rm -f -- "$1" '
                 '&& exec cat > "$1"')


def _read(tree: Path, rel: str, *, owners: Optional[set[int]], uid: Optional[int],
          limit: int) -> Optional[bytes]:
    """`tree/rel` read safely (see the module docstring), or None when it is not a regular,
    single-link file of the app's. _OverLimit past `limit` bytes."""
    try:
        fd = _open_regular(tree, rel, owners=owners)
    except PermissionError:
        if uid is None:
            return None
        out = _run_as(uid, _READ_AS_UID, str(tree / rel), str(limit + 1), timeout=60)
        if out is not None and len(out) > limit:
            raise _OverLimit()
        return out
    return None if fd is None else _read_fd(fd, limit)


def _candidates(tree: Path, *, owners: Optional[set[int]] = None,
                uid: Optional[int] = None) -> list[str]:
    """Relative paths of what may be the app's data: SQLite files (and their -wal/-shm/-journal)
    anywhere, and every file under a data folder. The walk only lists names (never following a
    link); what is READ is opened by `_read`."""
    names: set[str] = set()
    root = str(tree)
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS
                       and not os.path.islink(os.path.join(dirpath, d))]
        rel_dir = os.path.relpath(dirpath, root)
        parts = [] if rel_dir == "." else rel_dir.split(os.sep)
        for name in filenames:
            names.add(os.path.join(*parts, name) if parts else name)
    out: set[str] = set()
    for rel in sorted(names):
        parts = Path(rel).parts[:-1]
        if any(p in DATA_DIRS for p in parts):
            out.add(rel)
            continue
        head = _read_head(tree, rel, owners=owners, uid=uid)
        if head == SQLITE_MAGIC:
            out.add(rel)
            for suffix in SQLITE_SIDECARS:
                if rel + suffix in names:
                    out.add(rel + suffix)
    return sorted(out)


def _read_head(tree: Path, rel: str, *, owners: Optional[set[int]], uid: Optional[int]) -> bytes:
    try:
        fd = _open_regular(tree, rel, owners=owners)
    except PermissionError:
        if uid is None:
            return b""
        return _run_as(uid, _READ_AS_UID, str(tree / rel), "16", timeout=30) or b""
    if fd is None:
        return b""
    try:
        return os.read(fd, 16)
    except OSError:
        return b""
    finally:
        os.close(fd)


def _digest_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _inside(tree: Path, rel: str) -> Optional[Path]:
    """`tree/rel` when no component of it is a symlink (an app could point one at a BOSS file)."""
    if not _safe_rel(rel):
        return None
    current = tree
    for part in Path(rel).parts:
        current = current / part
        if current.is_symlink():
            return None
    return current


def describe(key: str) -> dict:
    """What is kept for an app: {"kept", "files", "bytes", "saved_at"}."""
    manifest = _store(key) / "manifest.json" if key else None
    if not manifest or not manifest.is_file():
        return {"kept": False, "files": 0, "bytes": 0, "saved_at": None}
    try:
        info = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"kept": False, "files": 0, "bytes": 0, "saved_at": None}
    return {"kept": True, "files": len(info.get("files") or []), "bytes": int(info.get("bytes") or 0),
            "saved_at": info.get("saved_at")}


def _write(tree: Path, rel: str, data: bytes, uid: Optional[int]) -> bool:
    """One restored file: as the app's uid (0640) in the sandbox, else by BOSS (0600), never
    through a link."""
    if _inside(tree, rel) is None:
        return False
    if uid is not None:
        return _run_as(uid, _WRITE_AS_UID, str(tree / rel), data=data) is not None
    from . import workspace_perms

    try:
        workspace_perms.write_nofollow(tree, rel, data, mode=0o600, trusted=_trusted_root(tree),
                                       dir_mode=0o700)
        return True
    except OSError:
        return False


def restore(tree: Path, key: str, *, uid: Optional[int] = None) -> list[str]:
    """Copy the app's kept data into `tree` (before the app runs). Returns the paths restored.
    `uid`: the preview's sandbox uid, which then owns what is written."""
    files_dir = _store(key) / "files" if key else None
    if not files_dir or not files_dir.is_dir():
        return []
    restored: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(files_dir, followlinks=False):
        for name in filenames:
            src = Path(dirpath) / name
            rel = os.path.relpath(src, files_dir)
            if src.is_symlink() or not _safe_rel(rel):
                continue
            try:
                data = src.read_bytes()
            except OSError:
                continue
            if _write(tree, rel, data, uid):
                restored.append(rel)
            else:
                logger.warning("preview data: %s not restored", rel)
    return restored


def mark_restored(preview_id: str, tree: Path, key: str, *, uid: Optional[int] = None) -> list[str]:
    """At launch, before the app runs: note the tree's baseline, restore the app's kept data, and
    remember where this preview's tree is (BOSS's store, never the tree: the app cannot rewrite
    it)."""
    if not key or not preview_id:
        return []
    baseline: dict[str, str] = {}
    limit = max_bytes()
    for rel in _candidates(tree):
        try:
            data = _read(tree, rel, owners=None, uid=None, limit=limit)
        except _OverLimit:
            continue
        if data is not None:
            baseline[rel] = _digest_of(data)
    restored = restore(tree, key, uid=uid)
    note = _tree_note(preview_id)
    try:
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(json.dumps({"key": key, "tree": str(tree), "baseline": baseline,
                                    "restored": restored, "at": time.time()}), encoding="utf-8")
    except OSError as exc:
        logger.warning("preview data: %s will not be kept (%s)", preview_id, type(exc).__name__)
    if restored:
        logger.info("preview data: %s restored %d file(s) for %s", preview_id, len(restored), key)
    return restored


def capture(preview_id: str, *, owner_uid: Optional[int] = None) -> dict:
    """When the preview ends, AFTER its uid is stopped: what its app wrote replaces the app's
    kept data. `owner_uid`: the stopped sandbox uid (None without a sandbox). Never raises."""
    try:
        return _capture(preview_id, owner_uid)
    except Exception as exc:  # noqa: BLE001 — keeping data must never fail a stop
        logger.warning("preview data: %s not kept (%s)", preview_id, type(exc).__name__)
        return {"kept": False, "why": "error"}


def _capture(preview_id: str, owner_uid: Optional[int]) -> dict:
    note = _tree_note(preview_id)
    if not note.is_file():
        return {"kept": False, "why": "no note"}
    info = json.loads(note.read_text(encoding="utf-8"))
    key = str(info.get("key") or "")
    tree = Path(str(info.get("tree") or ""))
    if not key or not str(info.get("tree") or "") or not tree.is_dir():
        note.unlink(missing_ok=True)
        return {"kept": False, "why": "no tree"}
    if in_sandbox(tree) and owner_uid is None:
        # A sandboxed tree whose uid nobody stopped may still be changing under us: never read.
        note.unlink(missing_ok=True)
        return {"kept": False, "why": "not stopped"}
    owners = {os.geteuid()} | ({owner_uid} if owner_uid is not None else set())
    baseline = info.get("baseline") or {}
    limit = max_bytes()
    wrote: list[tuple[str, bytes]] = []
    total = 0
    for rel in _candidates(tree, owners=owners, uid=owner_uid):
        try:
            data = _read(tree, rel, owners=owners, uid=owner_uid, limit=limit - total)
        except _OverLimit:
            total = limit + 1
            break
        if data is None:
            continue                         # not a regular, single-link file of the app's
        if baseline.get(rel) and baseline[rel] == _digest_of(data):
            continue                         # the repo's file, as the checkout had it
        wrote.append((rel, data))
        total += len(data)
    if total > limit:
        logger.warning("preview data: %s wrote over the %d-byte limit; the kept data is "
                       "unchanged (BOSS_PREVIEW_DATA_MAX_MB)", preview_id, limit)
        note.unlink(missing_ok=True)
        return {"kept": False, "why": "too large", "bytes": total}
    if not wrote:
        note.unlink(missing_ok=True)
        return {"kept": False, "why": "nothing written"}
    store = _store(key)
    stage = store.with_name(store.name + f".new-{os.getpid()}")
    shutil.rmtree(stage, ignore_errors=True)
    (stage / "files").mkdir(parents=True, mode=0o700)
    for rel, data in wrote:
        dst = stage / "files" / rel
        dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    copied = [rel for rel, _data in wrote]
    (stage / "manifest.json").write_text(json.dumps({
        "files": copied, "bytes": total, "saved_at": time.time(), "preview_id": preview_id}),
        encoding="utf-8")
    old = store.with_name(store.name + f".old-{os.getpid()}")
    if store.exists():
        os.replace(store, old)
    os.replace(stage, store)
    shutil.rmtree(old, ignore_errors=True)
    note.unlink(missing_ok=True)
    logger.info("preview data: %s kept %d file(s) for %s", preview_id, len(copied), key)
    return {"kept": True, "files": copied, "bytes": total, "key": key}


def reset(key: str) -> bool:
    """The operator's fresh data: delete what is kept for the app. True when something was."""
    if not key:
        return False
    store = _store(key)
    existed = store.exists()
    shutil.rmtree(store, ignore_errors=True)
    return existed


def forget(preview_id: str) -> None:
    """A preview that never served: nothing of it is kept."""
    with contextlib.suppress(OSError):
        _tree_note(preview_id).unlink(missing_ok=True)
