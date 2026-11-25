"""The worktree's content as one git tree sha, taken after a step that wrote files.

A ledger that records what a coder did but not what the files were afterwards cannot answer
"which step broke it" or put the tree back to the last good step (design §3.4). `git write-tree`
names the whole tree in one hash and costs no commit: it is written from a COPY of the index, so
the caller's real index, staging and history are untouched. `.boss/` (the plan and progress files
of §6.4) is left out, as it is left out of every diff.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Optional

#: A snapshot is a convenience: a repository too slow to hash is skipped, never waited on.
TIMEOUT_SECONDS = 30


def _git(cwd: Path, *args: str, index: Optional[str] = None) -> str:
    """One git on a run's worktree: AS THE RUN'S UID when the run holds one (owned_process wraps
    it through boss-sandbox-exec), never running what the tree configured (git_safety), pinned to
    `cwd`'s own real .git. BOSS's own uid writing objects here left .git/objects directories the
    run's uid could not add to (crun_47c3aea2); its hooks/filters would run as BOSS."""
    from ..services import owned_process
    from ..services.git_safety import NO_TREE_CODE, isolated_env

    if not stat.S_ISDIR(os.lstat(cwd / ".git").st_mode):
        raise OSError(f"{cwd}/.git is not a real directory")
    env = {**isolated_env({"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8"}),
           "GIT_DIR": str(cwd / ".git"), "GIT_WORK_TREE": str(cwd)}
    if index:
        env["GIT_INDEX_FILE"] = index
    done = owned_process.run(["git", *NO_TREE_CODE, *args], cwd=str(cwd), env=env,
                             capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    if done.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed")
    return done.stdout.strip()


def tree_sha(cwd: Path) -> Optional[str]:
    """The tree sha of everything in `cwd` that git would track (ignored files excluded), or
    None when `cwd` is not a git worktree (its own real .git) or git fails. Never raises.

    The temporary index lives in `cwd/.git` (where the run's uid writes) and starts empty: the
    caller's real index, staging and history are untouched."""
    import secrets

    try:
        cwd = Path(cwd)
        if not stat.S_ISDIR(os.lstat(cwd / ".git").st_mode):
            return None
    except OSError:
        return None
    index = str(cwd / ".git" / f"boss-tree-{secrets.token_hex(6)}.index")
    try:
        _git(cwd, "add", "--all", "--", ".", ":(exclude).boss", index=index)
        return _git(cwd, "write-tree", index=index) or None
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            os.unlink(index)
        except OSError:
            pass


__all__ = ["tree_sha"]
