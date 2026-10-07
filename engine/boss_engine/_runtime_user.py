"""BOSS CODE NEVER RUNS AS ROOT IN ITS OWN CONTAINER (round 3, R3-STACK S1).

The api container starts as root only so its entrypoint can fence the sandbox uids' network; then
it becomes `boss` for good (scripts/sandbox/entrypoint.sh). But compose's `user: "0:0"` also makes
root the default for `docker compose exec api …`, and anything run that way — install.sh's seed
step, an operator's `boss` CLI call, a support one-liner — wrote files as root into volumes the
service owns. The seed's artifacts (/var/lib/boss/artifacts/<project>/, root 0755) made the FIRST
build of every fresh install fail at its end with `Permission denied` on an artifact write.

A pointed fix (`-u boss` on one exec) leaves every other exec as the trap. So the image names its
runtime user (`BOSS_RUNTIME_USER=boss`, Dockerfile.community), and importing `boss_engine` as root
there becomes that user before any BOSS code runs: whatever starts BOSS code, it reads and writes
as the service does. Outside the image (a developer's laptop, CI) the variable is unset and nothing
changes. `BOSS_KEEP_ROOT=1` is the escape for a deliberate root maintenance command.
"""
from __future__ import annotations

import os
import sys

RUNTIME_USER_ENV = "BOSS_RUNTIME_USER"
KEEP_ROOT_ENV = "BOSS_KEEP_ROOT"


def become_runtime_user() -> bool:
    """Drop from root to the image's runtime user. True when it dropped."""
    name = (os.environ.get(RUNTIME_USER_ENV) or "").strip()
    if not name or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return False
    if (os.environ.get(KEEP_ROOT_ENV) or "").strip() == "1" or name == "root":
        return False
    import pwd

    try:
        pw = pwd.getpwnam(name)
    except KeyError:
        return False
    try:
        os.setgroups(os.getgrouplist(name, pw.pw_gid))
        os.setgid(pw.pw_gid)
        os.setuid(pw.pw_uid)
    except OSError as exc:
        # Never carry on as root silently: say so, and refuse (a root write is the bug this prevents).
        sys.stderr.write(f"boss: running as root and could not become {name!r} ({exc}); "
                         f"run as {name} (docker compose exec -u {name} …) or set {KEEP_ROOT_ENV}=1\n")
        raise SystemExit(1) from exc
    os.environ["HOME"] = pw.pw_dir
    os.environ["USER"] = os.environ["LOGNAME"] = name
    return True
