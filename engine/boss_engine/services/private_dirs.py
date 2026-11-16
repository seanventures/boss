"""BOSS's own directories are BOSS's alone (BOSSAPP-1325).

A code task's agent runs as an unprivileged pool uid (sandbox_identity) with no groups and umask
007. Its own run directory is <uid>:boss 2770, but anything BOSS itself wrote with the default
umask (0755 directories, 0644 files) was readable by that uid: the knowledge-graph index's clones
of every bound repository in /tmp/boss-kg-index, the chat tools' repo cache, the agent shell's
workspaces, the unisolated sandboxes and the shared npm cache. crun_bbf93235's planner, started
in an empty home, found /tmp/boss-kg-index/ghrepo_… — another project's clone — and planned
against it.

The rule, in one place:
  * a directory that holds project files BOSS shares across runs lives under `private_root()`
    (`/var/lib/boss/private`, created 0700 boss in the image), never in /tmp;
  * every such directory is 0700, owner BOSS (`private_dir`, `ensure_private`), whatever the
    umask and wherever it is configured;
  * what a sandboxed run must READ (Playwright's browsers) is opened explicitly, and nothing
    project-specific sits beside it.
"""
from __future__ import annotations

import contextlib
import os
import stat
import tempfile
import threading
from pathlib import Path
from typing import Optional

#: Where BOSS keeps shared clones, indexes and caches (owner-only).
PRIVATE_ROOT_ENV = "BOSS_PRIVATE_ROOT"
DEFAULT_PRIVATE_ROOT = "/var/lib/boss/private"


def ensure_private(path: Path | str) -> Path:
    """`path` created if missing and made owner-only (0700). Never raises for a directory BOSS
    cannot chmod (a read-only bind): the caller still gets the path."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(p, 0o700)
    return p


def private_root() -> Path:
    """BOSS's owner-only root: $BOSS_PRIVATE_ROOT, else /var/lib/boss/private when BOSS can
    make it, else a per-user directory in the system temp dir (a developer's machine)."""
    configured = os.environ.get(PRIVATE_ROOT_ENV, "").strip()
    if configured:
        return ensure_private(configured)
    try:
        root = ensure_private(DEFAULT_PRIVATE_ROOT)
        # MADE IS NOT USABLE (BOSSAPP-1327): with the new compose on an older image, Docker
        # creates /var/lib/boss/private (the import/selfsrc mount targets' parent) owned by root,
        # ensure_private cannot chmod it, and every private_dir() under it then raised
        # PermissionError. A root BOSS cannot write is no root: the per-user fallback.
        if os.access(root, os.W_OK | os.X_OK) and is_private(root):
            return root
    except OSError:
        pass
    return _fallback_root()


#: This process's own mkdtemp root, when the per-uid name in the temp dir is not ours.
_MKDTEMP_ROOT: Optional[Path] = None
_MKDTEMP_LOCK = threading.Lock()


def owned_private_dir(path: Path | str) -> bool:
    """Whether `path` is a real directory (not a symlink) owned by THIS uid and owner-only.
    `is_private` alone says nothing about WHO the owner is."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return (stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode)
            and st.st_uid == os.getuid() and is_private(path))


def _fallback_root() -> Path:
    """The per-uid directory in the temp dir, ONLY if it is ours (PR #166 review).

    /tmp is everyone's: another uid can pre-create /tmp/boss-private-<uid> (0777, or a symlink
    into its own tree) before BOSS starts, and BOSS then kept every project's clones in a
    directory that uid owns — a probe did exactly that. So after ensure_private the directory is
    lstat'ed: a real directory, owned by this uid, owner-only. Anything else is never used, and
    BOSS takes a fresh `mkdtemp` (0700, unguessable) for the life of the process instead."""
    global _MKDTEMP_ROOT
    candidate = Path(tempfile.gettempdir()) / f"boss-private-{os.getuid()}"
    if not os.path.islink(candidate):
        with contextlib.suppress(OSError):
            ensure_private(candidate)
        if owned_private_dir(candidate):
            return candidate
    with _MKDTEMP_LOCK:
        if _MKDTEMP_ROOT is None or not owned_private_dir(_MKDTEMP_ROOT):
            _MKDTEMP_ROOT = Path(tempfile.mkdtemp(prefix="boss-private-"))
        return _MKDTEMP_ROOT


def private_dir(name: str) -> Path:
    """A named owner-only directory under `private_root()` ("kg-index", "repo-cache")."""
    return ensure_private(private_root() / name)


def is_private(path: Path | str) -> bool:
    """Whether no one but the owner can enter `path` (group and others have no bits)."""
    try:
        return (os.stat(path).st_mode & 0o077) == 0
    except OSError:
        return False


__all__ = ["DEFAULT_PRIVATE_ROOT", "PRIVATE_ROOT_ENV", "ensure_private", "is_private",
           "owned_private_dir", "private_dir", "private_root"]
