"""The worktree's content as one git tree sha, taken after a step that wrote files.

A ledger that records what a coder did but not what the files were afterwards cannot answer
"which step broke it" or put the tree back to the last good step (design §3.4). `git write-tree`
names the whole tree in one hash and costs no commit: it is written from a COPY of the index, so
the caller's real index, staging and history are untouched. `.boss/` (the plan and progress files
of §6.4) is left out, as it is left out of every diff.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

#: A snapshot is a convenience: a repository too slow to hash is skipped, never waited on.
TIMEOUT_SECONDS = 30


def _git(cwd: Path, *args: str, env: Optional[dict] = None) -> str:
    done = subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True,
                          timeout=TIMEOUT_SECONDS, check=True)
    return done.stdout.strip()


def tree_sha(cwd: Path) -> Optional[str]:
    """The tree sha of everything in `cwd` that git would track (ignored files excluded), or
    None when `cwd` is not a git worktree or git fails. Never raises."""
    try:
        cwd = Path(cwd)
        git_dir = Path(_git(cwd, "rev-parse", "--absolute-git-dir"))
    except Exception:  # noqa: BLE001 — not a repository: nothing to snapshot
        return None
    handle, index = tempfile.mkstemp(prefix="boss-tree-", suffix=".index")
    os.close(handle)
    try:
        real_index = git_dir / "index"
        if real_index.exists():
            shutil.copyfile(real_index, index)  # its stat cache makes `add` fast
        else:
            os.unlink(index)
        env = {**os.environ, "GIT_INDEX_FILE": index}
        _git(cwd, "add", "--all", "--", ".", ":(exclude).boss", env=env)
        return _git(cwd, "write-tree", env=env) or None
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            os.unlink(index)
        except OSError:
            pass


__all__ = ["tree_sha"]
