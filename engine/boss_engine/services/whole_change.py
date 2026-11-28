"""THE PULL REQUEST'S WHOLE CHANGE, measured from where its branch left its base.

A follow-up or a repair works on the lineage's branch, so its own diff never shows what an earlier
attempt wrote (and a file it restored shows as a change). The change a pull request carries is
the tree against the MERGE BASE of its base branch and the work — never the base branch's tip:
a two-dot diff against `origin/main` counts every change other apps merged to main since as this
change's. Renames are off (`--no-renames`), so a file moved out of a folder reads as its deletion
there, not as a file that only appeared somewhere else. New, untracked files are added.

`deepen` is how a shallow checkout reaches the merge base: called between tries (the sandbox
fetches more history). None when no merge base is found: the caller then judges from the
lineage's own recorded paths, never from a two-dot diff.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

#: How many times a shallow checkout is deepened before BOSS gives up on the merge base.
DEEPEN_TRIES = 3


def _git(root: Path | str, *args: str):
    from .git_safety import tree_git

    return tree_git(root, *args, timeout=30)


def merge_base(root: Path | str, base_ref: str, *,
               deepen: Optional[Callable[[], None]] = None) -> Optional[str]:
    for attempt in range(DEEPEN_TRIES + 1):
        got = _git(root, "merge-base", base_ref, "HEAD")
        sha = (got.stdout or "").strip() if got is not None and got.returncode == 0 else ""
        if sha:
            return sha
        if deepen is None or attempt == DEEPEN_TRIES:
            return None
        try:
            deepen()
        except Exception:  # noqa: BLE001 — no more history to be had: no merge base
            return None
    return None


def paths_since_base(root: Path | str, base_ref: str, *,
                     deepen: Optional[Callable[[], None]] = None) -> Optional[list[str]]:
    """Every path the work in `root` (HEAD and the working tree) changes since it left
    `base_ref`. None when the merge base cannot be found or git cannot answer."""
    sha = merge_base(root, base_ref, deepen=deepen)
    if not sha:
        return None
    tracked = _git(root, "diff", "--name-only", "--no-renames", sha)
    new = _git(root, "ls-files", "--others", "--exclude-standard")
    if tracked is None or new is None or tracked.returncode != 0 or new.returncode != 0:
        return None
    return sorted({ln.strip() for ln in (tracked.stdout or "").splitlines()
                   + (new.stdout or "").splitlines() if ln.strip()})


__all__ = ["DEEPEN_TRIES", "merge_base", "paths_since_base"]
