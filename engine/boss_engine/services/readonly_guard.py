"""BOSS holds the read-only invariant itself when a reviewer runs without an inner sandbox.

Codex's own `--sandbox read-only` needs user namespaces a container does not grant
(`bwrap: No permissions to create a new namespace`, BOSSAPP-1350), so every command a reviewer
tried died and the seat judged blind. A reviewer seat on such an engine runs on a throwaway copy
(engine_contract `_isolated_copy`) with no inner sandbox, and this guard checks the LIVE tree it
was copied from: HEAD and the content of every tracked file are recorded before the turn and
compared after. A change is reverted and the seat fails with the reason.

Tracked files and HEAD only: untracked files are what a concurrent verification run legitimately
creates, and BOSS never reverts those.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Optional

#: Dirty tracked files whose bytes are kept to restore them (the change under review).
_KEEP_BYTES = 8_000_000


def _git(root: Path, *args: str) -> Optional[str]:
    try:
        out = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True,
                             timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _digest(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _dirty(root: Path) -> Optional[list[str]]:
    out = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=no")
    if out is None:
        return None
    paths = []
    parts = out.split("\0")
    for n, entry in enumerate(parts):
        if len(entry) > 3:
            paths.append(entry[3:])
        if entry[:1] in ("R", "C") and n + 1 < len(parts):
            parts[n + 1] = "  x"      # the rename's source path is not an entry of its own
    return paths


def snapshot(root: Path) -> Optional[dict]:
    """HEAD, and the content of every tracked file that is dirty; None outside a git tree."""
    root = Path(root)
    head = _git(root, "rev-parse", "HEAD")
    dirty = _dirty(root)
    if head is None or dirty is None:
        return None
    kept, budget = {}, _KEEP_BYTES
    for rel in dirty:
        try:
            data = (root / rel).read_bytes()
        except OSError:
            continue
        budget -= len(data)
        if budget >= 0:
            kept[rel] = data
    return {"head": head.strip(), "dirty": {r: _digest(root / r) for r in dirty}, "bytes": kept}


def enforce(root: Path, before: Optional[dict]) -> list[str]:
    """Revert whatever the turn changed in the live tree; the paths it changed ([] = clean)."""
    if not before:
        return []
    root = Path(root)
    changed: list[str] = []
    head = (_git(root, "rev-parse", "HEAD") or "").strip()
    if head and head != before["head"]:
        _git(root, "reset", "--soft", before["head"])
        changed.append("HEAD")
    now = _dirty(root) or []
    for rel in sorted(set(now) | set(before["dirty"])):
        was = before["dirty"].get(rel)
        if rel in before["dirty"] and _digest(root / rel) == was:
            continue
        if rel not in before["dirty"] and rel not in now:
            continue
        saved = before["bytes"].get(rel)
        if rel in before["dirty"] and saved is None:
            continue                              # too large to have kept; never guessed at
        if saved is not None:
            try:
                (root / rel).write_bytes(saved)
            except OSError:
                pass
        else:
            _git(root, "checkout", "HEAD", "--", rel)
        changed.append(rel)
    return changed
