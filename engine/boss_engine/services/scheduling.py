"""Where BOSS decides what may preempt what.

The control plane and the work it supervises run in the SAME container. `npm install`,
`vite build`, `tsc` and Playwright execute as child processes of the API server — and those are
among the most parallel workloads there are: esbuild and rollup deliberately use every core,
`npm install` writes tens of thousands of small files, Playwright launches real browsers. One
"job" is easily 8-30 runnable processes pinning every core and saturating disk IO.

Linux CFS shares CPU roughly equally per runnable thread at equal priority, so the API's handful
of threads end up competing with dozens of build processes. The API does not need more CPU — it
cannot get SCHEDULED. Measured in production: `/v1/code-tasks?limit=5` answers in **0.37s** on a
quiet box and took **47s** while a code task was running, after which `/health` could no longer
be scheduled inside its probe timeout, Docker marked the container unhealthy, and Traefik
dropped the route. That is the plain-text 404 behind "Reconnecting to the control plane".

Ruled out as the cause, by measurement rather than argument: the console's own ~90-call
server-render burst. With no job running it moves the same query from 0.37s to 0.78s (max
1.18s) and produces no 404s at all. It cannot account for 47s.

This is NOT a change to what runs or in what order. `nice` only sets scheduling WEIGHT under
contention — at nice 19 a process gets roughly 1/68th the weight of a normal one, so the API
preempts the build. With nothing else competing, the build runs exactly as fast as before.

The rule already existed, in `preview_service._low_priority`, whose docstring records this exact
diagnosis in these exact words — and it was applied to preview builds ONLY. The code agent and
the sandbox do the same work for far longer and got nothing. That is the S3 shape: one rule
understood in one place out of three. It lives here now so there is one definition to fix.
"""

from __future__ import annotations

import shutil

#: Lowest normal priority. The build should yield to anything that serves an operator.
NICE_LEVEL = "19"

#: Idle IO class. `npm install`'s file storm is as damaging as its CPU use — Postgres shares
#: this disk.
IONICE_CLASS = "3"


def low_priority(command: list[str]) -> list[str]:
    """Prefix `command` so it yields CPU and disk to the control plane.

    Degrades safely: `nice` is coreutils and effectively always present, `ionice` is not (it is
    absent on macOS and on some slim images), so it is added only when found. A missing tool
    must never stop the work from running — an unniced build is slow for the API, a failed
    build is a broken pipeline.
    """
    if not command:
        return command
    prefix = ["nice", "-n", NICE_LEVEL]
    if shutil.which("ionice"):
        prefix += ["ionice", "-c", IONICE_CLASS]
    return prefix + list(command)


def is_low_priority(command: list[str]) -> bool:
    """True when `command` has already been wrapped. Prevents double-prefixing when a caller
    passes an argv that another layer already niced."""
    return bool(command) and command[0] in ("nice", "ionice")
