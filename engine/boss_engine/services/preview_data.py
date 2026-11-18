"""AN APP'S PREVIEW DATA OUTLIVES THE PREVIEW (recipe-box, 2026-11-18).

A preview is a fresh checkout in a directory named after the preview id, so whatever the app
saved there (its SQLite file, its data folder) went when the preview did: stopped by BOSS after
20 idle minutes, restarted, and the operator's recipes were gone. The data belongs to the APP,
not to one preview of it, so it is kept per app (project + repository) in BOSS's own store,
outside every preview tree:

* `mark_restored` — at launch, before the app runs: the app's kept data is copied into the new
  tree, and what the tree held before that is noted (its baseline);
* `capture` — when the preview ends (stopped, idle, expired, or relaunched after a restart),
  after its processes are killed: what the app WROTE (SQLite files anywhere, and files under a
  data folder) that differs from the baseline replaces the app's kept data;
* `reset` — the operator's "fresh data": the app's kept data is deleted.

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


def _is_sqlite(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def _candidates(tree: Path) -> list[str]:
    """Relative paths of what may be the app's data: SQLite files (and their -wal/-shm/-journal)
    anywhere, and every file under a data folder. Never follows a symlink."""
    out: set[str] = set()
    root = str(tree)
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS
                       and not os.path.islink(os.path.join(dirpath, d))]
        rel_dir = os.path.relpath(dirpath, root)
        parts = [] if rel_dir == "." else rel_dir.split(os.sep)
        in_data = any(p in DATA_DIRS for p in parts)
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            rel = os.path.join(*parts, name) if parts else name
            if in_data or _is_sqlite(full):
                out.add(rel)
                if not in_data:
                    for suffix in SQLITE_SIDECARS:
                        side = full + suffix
                        if os.path.isfile(side) and not os.path.islink(side):
                            out.add(rel + suffix)
    return sorted(out)


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _safe_rel(rel: str) -> bool:
    return bool(rel) and not os.path.isabs(rel) and ".." not in Path(rel).parts


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


def restore(tree: Path, key: str, *, open_to_sandbox: bool = False) -> list[str]:
    """Copy the app's kept data into `tree`. Returns the paths restored."""
    files_dir = _store(key) / "files" if key else None
    if not files_dir or not files_dir.is_dir():
        return []
    restored: list[str] = []
    from .workspace_perms import usable_mode

    for dirpath, _dirnames, filenames in os.walk(files_dir, followlinks=False):
        for name in filenames:
            src = Path(dirpath) / name
            rel = os.path.relpath(src, files_dir)
            target = _inside(tree, rel)
            if target is None or src.is_symlink():
                continue
            try:
                made: list[Path] = []
                parent = target.parent
                while not parent.exists():
                    made.append(parent)
                    parent = parent.parent
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and not target.is_file():
                    continue
                with contextlib.suppress(FileNotFoundError):
                    target.unlink()
                shutil.copyfile(src, target)
                if open_to_sandbox:
                    os.chmod(target, usable_mode(0o644, is_dir=False))
                    for d in made:
                        with contextlib.suppress(OSError):
                            os.chmod(d, usable_mode(0o755, is_dir=True))
                restored.append(rel)
            except OSError as exc:
                logger.warning("preview data: %s not restored (%s)", rel, type(exc).__name__)
    return restored


def mark_restored(preview_id: str, tree: Path, key: str, *, open_to_sandbox: bool = False) -> list[str]:
    """At launch: note the tree's baseline, restore the app's kept data, and remember where this
    preview's tree is (BOSS's store, never the tree: the app cannot rewrite it)."""
    if not key or not preview_id:
        return []
    baseline: dict[str, str] = {}
    for rel in _candidates(tree):
        with contextlib.suppress(OSError):
            baseline[rel] = _digest(tree / rel)
    restored = restore(tree, key, open_to_sandbox=open_to_sandbox)
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


def capture(preview_id: str, tree: Optional[Path] = None) -> dict:
    """When the preview ends: what its app wrote replaces the app's kept data. Never raises."""
    try:
        return _capture(preview_id, tree)
    except Exception as exc:  # noqa: BLE001 — keeping data must never fail a stop
        logger.warning("preview data: %s not kept (%s)", preview_id, type(exc).__name__)
        return {"kept": False, "why": "error"}


def _capture(preview_id: str, tree: Optional[Path]) -> dict:
    note = _tree_note(preview_id)
    if not note.is_file():
        return {"kept": False, "why": "no note"}
    info = json.loads(note.read_text(encoding="utf-8"))
    key = str(info.get("key") or "")
    tree = tree or Path(str(info.get("tree") or ""))
    if not key or not str(tree) or not tree.is_dir():
        note.unlink(missing_ok=True)
        return {"kept": False, "why": "no tree"}
    baseline = info.get("baseline") or {}
    wrote: list[tuple[str, int]] = []
    total = 0
    for rel in _candidates(tree):
        full = tree / rel
        try:
            if baseline.get(rel) and baseline[rel] == _digest(full):
                continue                     # the repo's file, as the checkout had it
            size = full.stat().st_size
        except OSError:
            continue
        wrote.append((rel, size))
        total += size
    if not wrote:
        note.unlink(missing_ok=True)
        return {"kept": False, "why": "nothing written"}
    if total > max_bytes():
        logger.warning("preview data: %s wrote %d bytes, over the %d limit; the kept data is "
                       "unchanged (BOSS_PREVIEW_DATA_MAX_MB)", preview_id, total, max_bytes())
        note.unlink(missing_ok=True)
        return {"kept": False, "why": "too large", "bytes": total}
    store = _store(key)
    stage = store.with_name(store.name + f".new-{os.getpid()}")
    shutil.rmtree(stage, ignore_errors=True)
    (stage / "files").mkdir(parents=True)
    copied: list[str] = []
    for rel, _size in wrote:
        src = _inside(tree, rel)
        if src is None:
            continue
        dst = stage / "files" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(src, dst, follow_symlinks=False)
            copied.append(rel)
        except OSError as exc:
            logger.warning("preview data: %s unreadable (%s)", rel, type(exc).__name__)
    if not copied:
        shutil.rmtree(stage, ignore_errors=True)
        note.unlink(missing_ok=True)
        return {"kept": False, "why": "unreadable"}
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
