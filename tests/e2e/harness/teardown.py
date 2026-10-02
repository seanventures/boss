"""Remove everything a harness stack started on this machine, and prove nothing is left.

THE INCIDENT. After the first round of harness runs, `docker ps` on the developer's laptop still
showed four `boss-prev-prev-…` containers — each serving the harness's fake repo on 0.0.0.0 —
plus their images, and a `git remote-https … e2e-org/e2e-app.git` whose parent had died (ppid 1).
The run reports said nothing was left behind. Teardown only knew the process groups it had
spawned and the Postgres container it had named; the PRODUCT starts more than that (the preview
step runs `docker build`/`docker run` on the host; git and the code agent start their own
process groups), so none of it was found.

THE DESIGN: mark everything at the source, then remove by mark.
  * containers and images — every `docker` the API and worker run is tests/e2e/stubs/bin/docker,
    which labels what it creates `boss-e2e.run=<run id>`; the Postgres container carries the
    same label. Teardown removes every container and image with the label.
  * processes — every service runs with BOSS_E2E_RUN_ID=<run id> and HOME=<run dir>/home in its
    environment, which children inherit (and the scratch HOME survives the code agent's filtered
    environment). Teardown kills every process of this user carrying either, wherever it was
    re-parented.
  * the PIDs recorded at spawn are only signalled when the process is still the one spawned
    (same start time): a stale `down` must never kill a stranger that reused a PID.
Afterwards `leftovers()` looks again, and `scripts/e2e.sh all` fails if anything answers.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from . import E2E_ROOT

LABEL_KEY = "boss-e2e.run"
STUB_BIN = E2E_ROOT / "stubs" / "bin"


# ----------------------------------------------------------------------------- docker
def real_docker() -> Optional[str]:
    """The host's docker, never the harness's own shim."""
    explicit = os.environ.get("BOSS_E2E_REAL_DOCKER")
    if explicit:
        return explicit
    stub = str(STUB_BIN.resolve())
    rest = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep)
                           if p and str(Path(p).resolve()) != stub)
    return shutil.which("docker", path=rest)


def _docker(*args: str) -> list[str]:
    binary = real_docker()
    if not binary:
        return []
    try:
        done = subprocess.run([binary, *args], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


def labelled_containers(run_id: str) -> list[str]:
    return _docker("ps", "-aq", "--filter", f"label={LABEL_KEY}={run_id}")


def labelled_images(run_id: str) -> list[str]:
    return sorted(set(_docker("images", "-q", "--filter", f"label={LABEL_KEY}={run_id}")))


# ----------------------------------------------------------------------------- processes
def fingerprint(pid: int) -> Optional[str]:
    """When a live process started, or None. Taken at spawn, compared at stop: a PID and its
    start time name one process. Not the command: right after spawn it can still be the forked
    parent's, or a launcher that re-execs — the first self-test caught exactly that."""
    try:
        done = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    line = done.stdout.strip()
    return " ".join(line.split())[:200] if line else None


def _environments() -> dict[int, str]:
    """pid → its environment and command line, for this user's processes."""
    found: dict[int, str] = {}
    if Path("/proc").is_dir():
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                environ = (entry / "environ").read_bytes().replace(b"\0", b" ").decode(
                    errors="replace")
                cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                    errors="replace")
            except OSError:
                continue
            found[int(entry.name)] = f"{cmd} {environ} "
        return found
    # macOS: `ps -E` appends the environment to the command for the user's own processes.
    try:
        done = subprocess.run(["ps", "-E", "-ww", "-U", str(os.getuid()), "-o", "pid=",
                               "-o", "command="], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return found
    for line in done.stdout.splitlines():
        head, _, rest = line.strip().partition(" ")
        if head.isdigit():
            found[int(head)] = rest + " "
    return found


def marked_processes(run_id: str, run_dir: str) -> list[int]:
    """Every process of this user that carries the stack's run id or scratch HOME."""
    home = str(Path(run_dir) / "home")
    marks = [re.compile(r"(?:^|\s)BOSS_E2E_RUN_ID=" + re.escape(run_id) + r"\s"),
             re.compile(r"(?:^|\s)HOME=" + re.escape(home) + r"\s")]
    me = os.getpid()
    return sorted(pid for pid, text in _environments().items()
                  if pid != me and any(m.search(text) for m in marks))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate(pids: list[int], *, groups: bool) -> None:
    send = os.killpg if groups else os.kill
    for pid in pids:
        try:
            send(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.time() + 10
    while time.time() < deadline and any(_alive(p) for p in pids):
        time.sleep(0.2)
    for pid in pids:
        if _alive(pid):
            try:
                send(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


# ----------------------------------------------------------------------------- the whole
def stop(run_id: str, run_dir: str, pids: dict[str, int], fingerprints: dict[str, str],
         pg_container: str) -> list[str]:
    """Stop everything the stack started. Returns sentences about what was skipped."""
    notes: list[str] = []
    ours: list[int] = []
    for name, pid in pids.items():
        recorded = fingerprints.get(name)
        now = fingerprint(pid)
        if now is None:
            continue
        if recorded and now != recorded:
            notes.append(f"{name}: PID {pid} now belongs to another process ({now[:80]}); "
                         f"not signalled")
            continue
        ours.append(pid)
    _terminate(ours, groups=True)
    # Whatever escaped the process groups (git, the code agent, a preview server), found by
    # the mark it inherited. Twice: killing a parent can start a child's own shutdown.
    for _ in range(2):
        strays = marked_processes(run_id, run_dir)
        if strays:
            _terminate(strays, groups=False)
    containers = labelled_containers(run_id)
    if containers:
        _docker("rm", "-f", *containers)
    if pg_container:
        _docker("rm", "-f", pg_container)
    images = labelled_images(run_id)
    if images:
        _docker("rmi", "-f", *images)
    return notes


def leftovers(run_id: str, run_dir: str) -> list[str]:
    """What is still alive from this run after stop(): each as a sentence that says what to do."""
    found: list[str] = []
    for pid in marked_processes(run_id, run_dir):
        found.append(f"process {pid} from this run is still running (started "
                     f"{fingerprint(pid) or '?'}); "
                     f"stop it with `kill {pid}`")
    for cid in labelled_containers(run_id):
        found.append(f"container {cid} labelled {LABEL_KEY}={run_id} is still there; remove it "
                     f"with `docker rm -f {cid}`")
    for image in labelled_images(run_id):
        found.append(f"image {image} labelled {LABEL_KEY}={run_id} is still there; remove it with "
                     f"`docker rmi -f {image}`")
    return found


if __name__ == "__main__":  # pragma: no cover — `python -m e2e.harness.teardown <run_id> <dir>`
    print("\n".join(leftovers(sys.argv[1], sys.argv[2])) or "nothing left")
