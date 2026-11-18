"""BOSS WATCHES the live tree while a reviewer runs without an inner sandbox. It never acts on it.

Codex's own `--sandbox read-only` needs user namespaces a container does not grant
(`bwrap: No permissions to create a new namespace`, BOSSAPP-1350), so a review seat runs on a
throwaway copy (engine_contract `_isolated_copy`) with no inner sandbox. The live tree it was
copied from is fingerprinted before the turn and after: HEAD, and every changed or untracked file
(`git status --untracked-files=all`, from the repository's top level, wherever the seat's cwd is).

REPORT-ONLY, by design. The pipeline's own verification run writes into the same tree while the
seats sit (a lockfile, go.sum, a formatter, build output) and a pipeline step may commit, so
reverting or failing on a difference would undo BOSS's own work. A change is reported on the seat's
result; a change no concurrent verify explains (`explained`) marks the seat's verdict suspect.
"""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Optional

#: Files hashed per snapshot, and the largest one read. A tree past either is said so in the
#: report (`overflow`), never silently skipped.
MAX_FILES = 4000
MAX_BYTES = 5_000_000

#: What a concurrent verification run legitimately writes: not a reviewer's doing.
_EXPLAINED_NAMES = frozenset({
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb",
    "bun.lock", "poetry.lock", "Pipfile.lock", "uv.lock", "Cargo.lock", "Gemfile.lock",
    "composer.lock", "go.sum", "go.work.sum", "mix.lock", "pubspec.lock"})
_EXPLAINED_DIRS = frozenset({
    "node_modules", "dist", "build", "out", "target", "coverage", ".next", ".nuxt", ".cache",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv", ".gradle",
    "vendor", "test-results", "playwright-report", ".turbo", "htmlcov"})


def explained(path: str) -> bool:
    """True for a path a concurrent verify run is known to write."""
    parts = str(path).replace("\\", "/").split("/")
    return parts[-1] in _EXPLAINED_NAMES or any(p in _EXPLAINED_DIRS for p in parts[:-1]) \
        or parts[-1].endswith((".tsbuildinfo", ".pyc"))


class _Unverifiable(Exception):
    """The guard could not fingerprint a tree that IS a repository: never "unchanged"."""


def _top(cwd: Path) -> Optional[Path]:
    """The repository the seat stands in: the nearest ancestor of realpath(cwd) with a `.git`
    entry, or None outside any (no git tree: nothing to guard)."""
    try:
        here = Path(os.path.realpath(cwd))
    except OSError:
        return None
    for candidate in (here, *here.parents):
        try:
            os.lstat(candidate / ".git")
            return candidate
        except OSError:
            continue
    return None


def _runner(top: Path):
    """Who runs the guard's git: THE RUN'S UID when the tree is a sandboxed run's (owned_process
    wraps the spawn through boss-sandbox-exec, as sandbox._git_once does), BOSS itself only when
    no sandbox uid exists anywhere (a developer's machine: the agent is BOSS's uid too).
    Returns (sandboxed, why-not) — a run directory with no identity bound is unverifiable, never
    a reason to run the tree's git with BOSS's authority."""
    from . import owned_process, sandbox_identity

    ident = sandbox_identity.for_owner(owned_process.current_owner())
    if ident is not None:
        return True, None
    try:
        root = Path(os.path.realpath(sandbox_identity.run_root()))
        Path(top).relative_to(root)
        return None, "the tree is a sandboxed run's and no run identity is bound to check it"
    except (ValueError, OSError):
        return False, None


def _git(top: Path, *args: str, sandboxed: bool) -> str:
    """One READ-ONLY git on the tree at `top`, pinned to it.

    * AS THE RUN'S UID (`sandboxed`): `.git/config` is the run's to write, and `git status` runs
      core.fsmonitor, filter drivers (its own and its submodules'), and follows core.worktree.
      A git with the run's authority may: it has nothing the workload lacks. The git_safety
      overrides stay on as defence in depth (and are all there is on an unisolated machine,
      where the agent is BOSS's own uid).
    * PINNED: GIT_DIR=<top>/.git (a real directory, checked by the caller) and
      GIT_WORK_TREE=<top>, so core.worktree or a gitfile cannot point the guard elsewhere.
    * WATCHING NEVER WRITES (GIT_OPTIONAL_LOCKS=0): a plain `git status` REWRITES .git/index.
      crun_6883ec7c's came back boss 0600 (the backup's umask) and the run's own `git add -A`
      could no longer open it.
    Raises _Unverifiable on any failure."""
    from . import owned_process
    from .git_safety import NO_TREE_CODE, filter_overrides, isolated_env

    env = {**isolated_env({"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8"}),
           "GIT_OPTIONAL_LOCKS": "0", "GIT_DIR": str(top / ".git"), "GIT_WORK_TREE": str(top)}
    extra: list[str] = []
    if not sandboxed:
        extra = filter_overrides(top, env=env)
    argv = ["git", *NO_TREE_CODE, *extra, *args]
    if not sandboxed and args[:1] == ("status",):
        argv.append("--ignore-submodules=dirty")   # a submodule's own filters never run as BOSS
    try:
        done = owned_process.run(argv, cwd=str(top), capture_output=True, text=True, timeout=60,
                                 env=env, sandboxed=bool(sandboxed))
    except Exception as exc:  # noqa: BLE001
        raise _Unverifiable(f"git {args[0]} could not run: {type(exc).__name__}") from exc
    if done.returncode != 0:
        raise _Unverifiable(f"git {args[0]} failed: {(done.stderr or '').strip()[:200]}")
    return done.stdout


def is_repo(path: Path) -> bool:
    """The seat stands in a git tree (a `.git` entry above it): it is judged on a copy."""
    return _top(Path(path)) is not None


def _fingerprint(top_fd: int, rel: str) -> str:
    """sha256 of `rel` read WITHOUT following a link anywhere on its path (relative to an fd of
    the top, every component O_NOFOLLOW): a run's uid cannot point BOSS at a file of its own.
    A symlink is fingerprinted by its target text."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    parts = [p for p in rel.split("/") if p]
    fd = os.dup(top_fd)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, flags | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = nxt
        st = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        if stat.S_ISLNK(st.st_mode):
            return "link:" + os.readlink(parts[-1], dir_fd=fd)
        if not stat.S_ISREG(st.st_mode):
            return "special"
        if st.st_size > MAX_BYTES:
            return f"size:{st.st_size}"
        ffd = os.open(parts[-1], flags | os.O_NONBLOCK, dir_fd=fd)
        try:
            digest = hashlib.sha256()
            while True:
                chunk = os.read(ffd, 1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
            return digest.hexdigest()
        finally:
            os.close(ffd)
    finally:
        os.close(fd)


def snapshot(cwd: Path) -> Optional[dict]:
    """HEAD and a fingerprint of every dirty or untracked file. None ONLY outside a git tree; a
    repository the guard cannot read comes back {"unverifiable": why} — never "nothing to see"."""
    top = _top(Path(cwd))
    if top is None:
        return None
    try:
        git_dir = os.lstat(top / ".git")
        if not stat.S_ISDIR(git_dir.st_mode):
            raise _Unverifiable(".git is not a real directory (a symlink or a gitfile)")
        sandboxed, why = _runner(top)
        if sandboxed is None:
            raise _Unverifiable(why)
        shown = _git(top, "rev-parse", "--show-toplevel", sandboxed=sandboxed).strip()
        if os.path.realpath(shown) != str(top):
            raise _Unverifiable(f"git names {shown} as the tree, not {top}")
        try:
            head = _git(top, "rev-parse", "HEAD", sandboxed=sandboxed).strip()
        except _Unverifiable:
            head = ""                                   # a repository with no commit yet
        listing = _git(top, "status", "--porcelain=v1", "-z", "--untracked-files=all",
                       sandboxed=sandboxed)
    except _Unverifiable as exc:
        return {"root": str(top), "unverifiable": str(exc)}
    paths, parts = [], listing.split("\0")
    skip = False
    for entry in parts:
        if skip:
            skip = False
            continue
        if len(entry) > 3:
            paths.append(entry[3:])
            skip = entry[0] in ("R", "C")     # a rename's source is not an entry of its own
    prints: dict[str, str] = {}
    overflow = 0
    try:
        top_fd = os.open(str(top), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        return {"root": str(top), "unverifiable": f"the tree could not be opened: {exc}"}
    try:
        for rel in paths:
            if len(prints) >= MAX_FILES:
                overflow += 1
                continue
            try:
                prints[rel] = _fingerprint(top_fd, rel.rstrip("/"))
            except OSError:
                prints[rel] = "absent"
    finally:
        os.close(top_fd)
    return {"root": str(top), "head": head, "prints": prints, "overflow": overflow}


def compare(before: Optional[dict]) -> Optional[dict]:
    """What changed in the live tree since `before`, or None when nothing did (or there was no
    git tree to guard). A tree the guard could not read, before or after, is UNVERIFIABLE:
    reported, and the seat's verdict marked suspect — never read as "unchanged".

    {"paths": [...], "unexplained": [...], "head_moved": bool, "overflow": int,
     "unverifiable": str (only when it is)}"""
    if not before:
        return None
    after = snapshot(Path(before["root"]))
    problem = before.get("unverifiable") or (
        "the tree is no longer a git repository" if after is None else after.get("unverifiable"))
    if problem:
        return {"paths": [], "unexplained": [], "head_moved": False, "overflow": 0,
                "unverifiable": problem}
    changed = sorted(p for p in set(before["prints"]) | set(after["prints"])
                     if before["prints"].get(p) != after["prints"].get(p))
    moved = bool(before["head"]) and after["head"] != before["head"]
    overflow = int(before.get("overflow") or 0) + int(after.get("overflow") or 0)
    if not changed and not moved and not overflow:
        return None
    return {"paths": changed[:50], "unexplained": [p for p in changed if not explained(p)][:50],
            "head_moved": moved, "overflow": overflow}


def words(report: dict) -> str:
    bits = []
    if report.get("unverifiable"):
        bits.append("the live tree could not be checked during review: " + report["unverifiable"])
    if report.get("paths"):
        bits.append("files changed in the live tree during review: "
                    + ", ".join(report["paths"][:8]))
    if report.get("head_moved"):
        bits.append("HEAD moved")
    if report.get("overflow"):
        bits.append(f"{report['overflow']} file(s) past the guard's limit were not compared")
    return "; ".join(bits)
