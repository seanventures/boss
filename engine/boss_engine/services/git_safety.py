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
import subprocess
from pathlib import Path
from typing import Mapping, Optional

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


__all__ = ["ISOLATED_ENV", "NO_TREE_CODE", "filter_overrides", "isolated_env"]
