"""GIT THAT RUNS WITH BOSS'S AUTHORITY NEVER RUNS WHAT A TREE CONFIGURED (BOSSAPP-1325).

A run's uid can write its checkout's `.git/config` and `.gitattributes`. Git started by BOSS's own
uid on that checkout (provisioning, a workspace with no sandbox uid, the read-only guard that
fingerprints a reviewer's live tree) must not execute anything the tree names:

* hooks (`core.hooksPath=/dev/null`) and the filesystem monitor (`core.fsmonitor=false`): `git
  status` runs the fsmonitor command;
* filter drivers: `git status`/`git add` run a `filter.<name>.clean` (or `.process`) command
  for any path `.gitattributes` maps to it. Every driver the config defines is emptied for the
  call, which git treats as "no filter";
* no system or global config, no terminal prompt.

DEFENCE IN DEPTH, NOT THE BOUNDARY. A tree's owner can rewrite its config between the read that
lists its filters and the git that would run them, and a submodule brings config of its own, so
no list of overrides closes this while the git runs as BOSS. The boundary is WHO runs it: git on
a sandboxed run's tree runs AS THE RUN'S UID (sandbox._git_once after provisioning, the warm
reuse, readonly_guard._git). These options stay on that git anyway, and are what an unisolated
machine (no sandbox uid: the agent is BOSS's own uid there) has.

NO_TREE_CODE is used by sandbox._git_once and readonly_guard._git; filter_overrides by
readonly_guard._git when it runs as BOSS.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import Any, Mapping, Optional

#: The `-c` options every BOSS-authority git on a run's tree carries.
NO_TREE_CODE: tuple[str, ...] = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")

#: The environment that keeps system/global config and prompts out of it.
ISOLATED_ENV: Mapping[str, str] = {
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_ATTR_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
}


def isolated_env(base: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """`base` (default: this process's environment) with ISOLATED_ENV over it."""
    return {**(os.environ if base is None else base), **ISOLATED_ENV}


def filter_overrides(root: Path, *, env: Mapping[str, str], timeout: float = 20) -> list[str]:
    """`-c filter.<name>.{clean,smudge,process}=` and `.required=false` for every
    filter driver the tree's config defines. Reading config executes nothing. [] when git cannot
    say (no repository)."""
    try:
        done = subprocess.run(["git", "config", "--null", "--name-only", "--get-regexp",
                               r"^filter\."], cwd=str(root), capture_output=True, text=True,
                              timeout=timeout, env=dict(env))
    except (OSError, subprocess.SubprocessError):
        return []
    names = set()
    for key in filter(None, done.stdout.split("\0")):
        parts = key.split(".")
        if len(parts) >= 3:
            names.add(".".join(parts[1:-1]))
    out: list[str] = []
    for name in sorted(names):
        for what in ("clean", "smudge", "process"):
            out += ["-c", f"filter.{name}.{what}="]
        out += ["-c", f"filter.{name}.required=false"]
    return out


#: What a read-only git on a run's tree never reads from the tree's config: an excludes file it
#: names (core.excludesFile pointed at a BOSS file is an oracle for that file's lines).
READ_ONLY_CONFIG: tuple[str, ...] = ("-c", "core.excludesFile=/dev/null", "-c", "safe.directory=*")

def tree_git(root: Any, *args: str, owner: Any = None, timeout: float = 20,
             text: bool = True) -> Optional[subprocess.CompletedProcess]:
    """A READ-ONLY git (rev-parse, ls-files, merge-base, remote get-url, ...) BOSS runs on a
    run's tree. None when refused or git could not start; otherwise the finished process.

    * AS THE RUN'S UID when `owner` (default: the enclosing owner scope) holds a sandbox
      identity, through owned_process like sandbox._git_once: whatever the tree planted
      (objects/info/alternates naming another repository, a core.excludesFile naming a BOSS
      file) is read with the run's own authority, which has nothing BOSS's has.
    * Pinned to `root`'s own real .git (GIT_DIR/GIT_WORK_TREE): a `.git` that is a link or a
      gitfile is refused, never discovered upwards.
    * NO_TREE_CODE, READ_ONLY_CONFIG, no system/global config, GIT_OPTIONAL_LOCKS=0 (no
      opportunistic index rewrite).
    * No identity held and the tree inside the sandbox root: refused outright. Checking it for
      another object store (objects/info/alternates, commondir, a linked objects/) and then
      running as BOSS would race the run's uid, and git has no switch to ignore those."""
    from . import owned_process, sandbox_identity

    base = Path(str(root))
    git_dir = base / ".git"
    try:
        if not stat.S_ISDIR(os.lstat(git_dir).st_mode):
            return None
    except OSError:
        return None
    holder = owner if owner is not None else owned_process.current_owner()
    ident = sandbox_identity.for_owner(holder)
    if ident is None:
        from . import workspace_perms

        if workspace_perms.trusted_root_for(base) is not None:
            return None       # a sandboxed run's tree is only ever read with its run's authority
    env = {**isolated_env({"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8",
                           "HOME": os.devnull}),
           "GIT_DIR": str(git_dir), "GIT_WORK_TREE": str(base), "GIT_OPTIONAL_LOCKS": "0"}
    try:
        return owned_process.run(["git", *NO_TREE_CODE, *READ_ONLY_CONFIG, *args],
                                 cwd=str(base), env=env, capture_output=True, text=text,
                                 timeout=timeout, **({"owner": holder} if holder is not None
                                                     else {}),
                                 **({} if ident is not None else {"sandboxed": False}))
    except (OSError, subprocess.SubprocessError):
        return None
    except Exception:  # noqa: BLE001 — e.g. SandboxUnavailable: no read, never BOSS's uid instead
        return None


__all__ = ["ISOLATED_ENV", "NO_TREE_CODE", "READ_ONLY_CONFIG", "filter_overrides",
           "isolated_env", "tree_git"]
