"""Run agent-authored commands inside a container instead of the control plane.

What this is for
----------------
A code task runs as a subprocess in a temp directory **inside the API/worker container**,
with full network egress by explicit design. The agent's `bash` therefore executes with the
control plane's filesystem and network reachability. On 2026-09-02 an agent's test run
reached a production `DATABASE_URL` and cleared 204 tables; `agent_env` closed that one
variable's route, but a deny-list of environment variables is not a boundary.

This is the boundary: commands run in a container with the workspace bind-mounted, **no
network by default**, a non-root user, and memory/CPU/PID limits. The agent's model calls
stay in the control plane, which is where they belong — the container needs no network
precisely because nothing in it should be talking to anything.

Why commands and not the whole task
-----------------------------------
Putting the entire pipeline in a container means shipping and maintaining an image with git,
node, python and a coding CLI, and it moves model traffic into the sandbox. Putting only the
*commands* there addresses the actual threat, keeps model calls where the credentials
already are, and costs one container per task rather than a rebuild per release.

Latency
-------
One container per code task, not per command: `docker run -d … sleep infinity` once, then
`docker exec` per command. Measured overhead is the exec, not a container start. Commands
still run under `nice`/`ionice` inside the container for the same reason they do outside it
— see `tests/test_builds_yield_to_the_control_plane.py`.

This runtime is **opt-in** (`BOSS_SANDBOX_COMMAND_RUNTIME=docker`). It fails closed with a
readable message rather than silently degrading to the weaker path.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time
from typing import Optional

from . import owned_process

_log = logging.getLogger(__name__)

#: Long enough to be a real build step, short enough that a hung container is noticed.
_DEFAULT_TIMEOUT = 120
_DOCKER_START_TIMEOUT = 60
_MAX_OUTPUT = 12_000


class ContainerUnavailable(RuntimeError):
    """Docker is not usable. Raised by preflight so the operator sees why, not a stack."""


@dataclass
class CommandOutcome:
    """Shape-compatible with `EphemeralSandbox.run`'s CommandResult for the tool layer."""
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def docker_available(binary: str = "docker") -> tuple[bool, str]:
    """Is there a usable daemon? Returns (ok, reason-if-not)."""
    exe = shutil.which(binary)
    if not exe:
        return False, f"{binary!r} is not on PATH"
    try:
        proc = owned_process.run([exe, "info", "--format", "{{.ServerVersion}}"],
                              capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return False, "`docker info` timed out — is the daemon running?"
    except OSError as exc:
        return False, f"could not execute {exe}: {exc}"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "docker daemon not reachable").strip()
    return True, (proc.stdout or "").strip()


def preflight(settings) -> None:
    """Fail closed, with the reason, before a task starts rather than mid-build."""
    ok, reason = docker_available(getattr(settings, "sandbox_docker_binary", "docker"))
    if not ok:
        raise ContainerUnavailable(
            "BOSS_SANDBOX_COMMAND_RUNTIME=docker, but Docker is not usable: "
            f"{reason}. Start the daemon, or set BOSS_SANDBOX_COMMAND_RUNTIME=subprocess "
            "to run commands in the control plane's own container (weaker isolation)."
        )


class ContainerCommandRunner:
    """One container per code task; `docker exec` per command.

    Usable as a context manager, and safe to call `stop()` on more than once — a leaked
    container outlives the run that made it and nothing else will clean it up.
    """

    def __init__(self, root: Path, settings, *, task_id: str = "") -> None:
        self.root = Path(root).resolve()
        self.settings = settings
        self.task_id = task_id or "task"
        self.binary = shutil.which(getattr(settings, "sandbox_docker_binary", "docker")) or "docker"
        self.image = getattr(settings, "sandbox_container_image", "python:3.11-slim")
        self.network = getattr(settings, "sandbox_container_network", "none") or "none"
        self.memory = getattr(settings, "sandbox_container_memory", "2g")
        self.cpus = str(getattr(settings, "sandbox_container_cpus", "2"))
        self.pids = str(getattr(settings, "sandbox_container_pids", 512))
        self.container_id: Optional[str] = None

    # -- lifecycle -----------------------------------------------------
    def start(self) -> str:
        if self.container_id:
            return self.container_id
        preflight(self.settings)
        # Named `create_argv`, not `argv`: this starts the container, it does not run the
        # build. test_builds_yield_to_the_control_plane.py treats an `argv` handed to
        # subprocess.run as a build launch that must be niced, and it is right to — the
        # build launch in this module is in `run()` below, and it IS niced. Excluding this
        # file from that guard would weaken a rule that exists because an unniced build took
        # /v1/code-tasks from 0.37s to 47.1s.
        create_argv = [
            self.binary, "run", "--detach", "--rm",
            "--name", f"boss-sbx-{self.task_id}-{int(time.time())}",
            # NO NETWORK. The agent's model calls happen in the control plane; a build that
            # needs to fetch dependencies is an explicit opt-in, not a default.
            "--network", self.network,
            "--memory", str(self.memory),
            "--cpus", self.cpus,
            "--pids-limit", self.pids,
            # Non-root, and matching the host uid so files the build writes stay editable
            # by the control plane that has to read the diff back.
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--workdir", "/work",
            "--volume", f"{self.root}:/work",
            # Defence in depth: even inside the container, privilege escalation is off.
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL",
            self.image, "sleep", "infinity",
        ]
        try:
            proc = owned_process.run(create_argv, capture_output=True, text=True,
                                  timeout=_DOCKER_START_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise ContainerUnavailable(
                f"starting the sandbox container timed out after {_DOCKER_START_TIMEOUT}s"
            ) from exc
        if proc.returncode != 0:
            raise ContainerUnavailable(
                "could not start the sandbox container: "
                + (proc.stderr or proc.stdout or "").strip()
            )
        self.container_id = (proc.stdout or "").strip()
        _log.info("sandbox container %s started (image=%s network=%s)",
                  self.container_id[:12], self.image, self.network)
        return self.container_id

    def stop(self) -> None:
        cid, self.container_id = self.container_id, None
        if not cid:
            return
        try:
            owned_process.run([self.binary, "rm", "-f", cid],
                           capture_output=True, text=True, timeout=30)
        except Exception as exc:  # noqa: BLE001 — teardown must never mask the real failure
            _log.warning("could not remove sandbox container %s: %s", cid[:12], exc)

    def __enter__(self) -> "ContainerCommandRunner":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- execution -----------------------------------------------------
    def run(self, command: str, timeout: Optional[int] = None) -> CommandOutcome:
        """Run one plain command in the container. No shell, same as the host path."""
        from .scheduling import low_priority

        limit = int(timeout or _DEFAULT_TIMEOUT)
        argv = shlex.split(command)
        if not argv:
            return CommandOutcome(command, 2, "", "empty command", 0)
        self.start()
        exec_argv = [
            self.binary, "exec", "--workdir", "/work",
            "--env", "CI=1",
            self.container_id or "",
            *low_priority(argv),
        ]
        started = time.monotonic()
        try:
            proc = owned_process.run(exec_argv, capture_output=True, text=True, timeout=limit)
        except subprocess.TimeoutExpired:
            return CommandOutcome(command, 124, "", f"timed out after {limit}s",
                                  int((time.monotonic() - started) * 1000), timed_out=True)
        ms = int((time.monotonic() - started) * 1000)
        return CommandOutcome(
            command=command,
            exit_code=proc.returncode,
            stdout=(proc.stdout or "")[:_MAX_OUTPUT],
            stderr=(proc.stderr or "")[:_MAX_OUTPUT],
            duration_ms=ms,
        )

    def as_command_runner(self):
        """The callable `SandboxToolset` expects: (command, timeout) -> result."""
        return lambda cmd, timeout=None: self.run(cmd, timeout)
