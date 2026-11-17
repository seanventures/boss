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
import subprocess
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


def _git(root: Path, *args: str) -> Optional[str]:
    try:
        out = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True,
                             timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def is_repo(path: Path) -> bool:
    return _git(Path(path), "rev-parse", "--show-toplevel") is not None


def snapshot(cwd: Path) -> Optional[dict]:
    """HEAD and a fingerprint of every dirty or untracked file; None outside a git tree."""
    top = _git(Path(cwd), "rev-parse", "--show-toplevel")
    head = _git(Path(cwd), "rev-parse", "HEAD")
    if top is None:
        return None
    root = Path(top.strip())
    listing = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if listing is None:
        return None
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
    for rel in paths:
        if len(prints) >= MAX_FILES:
            overflow += 1
            continue
        try:
            file = root / rel
            if file.is_file() and file.stat().st_size > MAX_BYTES:
                prints[rel] = f"size:{file.stat().st_size}"
            else:
                prints[rel] = hashlib.sha256(file.read_bytes()).hexdigest()
        except OSError:
            prints[rel] = "absent"
    return {"root": str(root), "head": (head or "").strip(), "prints": prints,
            "overflow": overflow}


def compare(before: Optional[dict]) -> Optional[dict]:
    """What changed in the live tree since `before`, or None when nothing did.

    {"paths": [...], "unexplained": [...], "head_moved": bool, "overflow": int}"""
    if not before:
        return None
    after = snapshot(Path(before["root"]))
    if after is None:
        return None
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
    if report.get("paths"):
        bits.append("files changed in the live tree during review: "
                    + ", ".join(report["paths"][:8]))
    if report.get("head_moved"):
        bits.append("HEAD moved")
    if report.get("overflow"):
        bits.append(f"{report['overflow']} file(s) past the guard's limit were not compared")
    return "; ".join(bits)
