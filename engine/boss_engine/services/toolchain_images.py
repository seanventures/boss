"""Custom build-sandbox images, built interactively (BOSSAPP-825).

BOSS ships one build image (BOSS_SANDBOX_CONTAINER_IMAGE) and `toolchains.py`
only knows the stacks Dockerfile.community can switch on. A project that needs a
JDK, Go, browsers or a private CLI either reinstalled it on every run or failed.
This module lets an operator build that project's image ONCE, by hand, and has
every later build use it:

    start_session  ->  exec (the console terminal, any number of times)
                   ->  begin_capture  ->  finish_capture   ->  ready
                   \\-> discard                             ->  failed

* **The setup session** is a throwaway container from the base image, started
  with the same docker CLI and labels ContainerCommandRunner uses (so the
  resource reaper's `boss.owner` sweep can find a leak). Unlike a build
  container it runs as root with network (default `bridge`): installing tools
  is the whole point. Every command is audited and kept in the transcript.
* **Capture** is `docker commit`, then — when BOSS_TOOLCHAIN_REGISTRY names a
  registry (the compose `toolchains` profile runs one on the loopback) — a
  `docker push`, whose manifest digest is recorded. Without a registry the image
  stays in the local daemon and its image id is the digest. The setup container
  is removed either way. The transcript is also written as a Dockerfile, so the
  image can be reproduced without the console.
* **Builds use it**: `runner_image_for` answers ContainerCommandRunner's question
  "which image for this task?" — an execution environment's `config.image`
  first (BOSSAPP-824; the lease fingerprint already keys on it), then the
  project's latest READY toolchain image, else None, which means the configured
  default exactly as before.

Everything here requires BOSS_SANDBOX_COMMAND_RUNTIME=docker: a captured image
is only ever used by the docker runtime, and starting a root container with
network on an install that chose not to run docker would be a surprise.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from typing import Any, Callable, Optional

from sqlalchemy import text

from .core import emit_event, make_id

_log = logging.getLogger(__name__)

STATUSES = ("building", "ready", "failed")
PHASE_SESSION = "session"
PHASE_CAPTURING = "capturing"

_SESSION_START_TIMEOUT = 300      # the base image may need pulling
_EXEC_TIMEOUT = 900               # an apt-get / sdk install is slow, not hung
_COMMIT_TIMEOUT = 900
_PUSH_TIMEOUT = 1800
_MAX_OUTPUT = 20_000
_MAX_TRANSCRIPT = 500

#: A docker CLI call: (argv, timeout, stdin) -> CompletedProcess. Injected by tests.
DockerRun = Callable[..., "subprocess.CompletedProcess[str]"]


class ToolchainImageError(ValueError):
    """Phrased for the operator; the API maps it to 409/422."""


class DockerRuntimeOff(ToolchainImageError):
    """The install does not run build commands in docker, so an image is moot."""


class ToolchainImageNotFound(LookupError):
    pass


# ── docker plumbing ──────────────────────────────────────────────────────────


def _binary(settings: Any) -> str:
    import shutil

    name = getattr(settings, "sandbox_docker_binary", "docker") or "docker"
    return shutil.which(name) or name


def _default_docker(argv: list[str], timeout: int, stdin: Optional[str] = None):
    from . import owned_process

    return owned_process.boss_run(argv, capture_output=True, text=True,
                                  timeout=timeout, input=stdin)


def _docker(run: Optional[DockerRun]) -> DockerRun:
    return run or _default_docker


def _out(proc: Any) -> str:
    return ((getattr(proc, "stdout", "") or "") + (getattr(proc, "stderr", "") or "")).strip()


def docker_runtime_on(settings: Any) -> bool:
    return str(getattr(settings, "sandbox_command_runtime", "") or "").strip().lower() == "docker"


def require_docker_runtime(settings: Any) -> None:
    if not docker_runtime_on(settings):
        runtime = str(getattr(settings, "sandbox_command_runtime", "") or "unset")
        raise DockerRuntimeOff(
            "Toolchain images are used by the docker build runtime, and this install runs "
            f"build commands with BOSS_SANDBOX_COMMAND_RUNTIME={runtime}. Set it to 'docker' "
            "(and restart the API and worker) to build and use a project image.")


def registry(settings: Any) -> str:
    return str(getattr(settings, "toolchain_registry", "") or "").strip().rstrip("/")


def image_repository(settings: Any, project_id: str) -> str:
    """`[<registry>/]boss-toolchain/<project>` — lower-case, docker-safe."""
    safe = re.sub(r"[^a-z0-9._-]+", "-", str(project_id).lower()).strip("-.") or "project"
    repo = f"boss-toolchain/{safe}"
    reg = registry(settings)
    return f"{reg}/{repo}" if reg else repo


# ── rows ─────────────────────────────────────────────────────────────────────


def _jsonb(connection) -> str:
    try:
        name = connection.engine.dialect.name
    except AttributeError:
        name = "postgresql"
    return ":%s" if name == "sqlite" else "CAST(:%s AS JSONB)"


def _row(row: Any) -> dict:
    out = dict(row)
    raw = out.get("transcript")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = []
    out["transcript"] = raw if isinstance(raw, list) else []
    return out


def get_image(connection, image_id: str) -> dict:
    row = connection.execute(text("SELECT * FROM toolchain_images WHERE id = :id"),
                             {"id": image_id}).mappings().first()
    if row is None:
        raise ToolchainImageNotFound(f"no toolchain image {image_id!r}.")
    return _row(row)


def list_images(connection, project_id: str, *, limit: int = 20) -> list[dict]:
    rows = connection.execute(text(
        "SELECT * FROM toolchain_images WHERE project_id = :p "
        "ORDER BY created_at DESC LIMIT :n"), {"p": project_id, "n": int(limit)}).mappings().all()
    return [_row(r) for r in rows]


def ready_image(connection, project_id: str) -> Optional[dict]:
    """The project's latest READY image row, or None."""
    row = connection.execute(text(
        "SELECT * FROM toolchain_images WHERE project_id = :p AND status = 'ready' "
        "AND image_ref IS NOT NULL ORDER BY created_at DESC LIMIT 1"),
        {"p": project_id}).mappings().first()
    return _row(row) if row is not None else None


def public_view(row: dict, settings: Any = None) -> dict:
    transcript = row.get("transcript") or []
    container = str(row.get("container_id") or "")
    binary = getattr(settings, "sandbox_docker_binary", "docker") if settings else "docker"
    return {
        "id": row.get("id"),
        "project_id": row.get("project_id"),
        "base": row.get("base"),
        "status": row.get("status"),
        "phase": row.get("phase"),
        "image_ref": row.get("image_ref"),
        "digest": row.get("digest"),
        "error": row.get("error"),
        "created_by": row.get("created_by"),
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
        "commands": len(transcript),
        "transcript": transcript[-50:],
        "dockerfile": row.get("dockerfile"),
        # For an operator on the BOSS host who prefers a real TTY to the console.
        "shell_command": (f"{binary} exec -it {container[:12]} sh"
                          if container and row.get("status") == "building" else None),
    }


def _set(connection, image_id: str, **fields: Any) -> None:
    assignments, params = [], {"id": image_id}
    for key, value in fields.items():
        if key == "transcript":
            assignments.append(f"transcript = {_jsonb(connection) % 'transcript'}")
            params["transcript"] = json.dumps(value)
        else:
            assignments.append(f"{key} = :{key}")
            params[key] = value
    assignments.append("updated_at = now()")
    connection.execute(text(f"UPDATE toolchain_images SET {', '.join(assignments)} WHERE id = :id"),
                       params)


def _require_open(row: dict) -> None:
    if row.get("status") != "building" or row.get("phase") != PHASE_SESSION:
        raise ToolchainImageError(
            f"toolchain image {row.get('id')} is {row.get('status')}"
            f"{' (' + str(row.get('phase')) + ')' if row.get('status') == 'building' else ''}"
            " — its setup session is not open. Start a new one.")


def dockerfile_for(base: str, transcript: list[dict]) -> str:
    """The transcript as a Dockerfile: FROM base, one RUN per command that
    succeeded. Reproduces the image without the console (logins excepted)."""
    lines = [f"# Built interactively in BOSS (BOSSAPP-825); replays the setup session.",
             f"FROM {base}"]
    for entry in transcript:
        if int(entry.get("exit_code", 1)) == 0 and str(entry.get("command") or "").strip():
            lines.append(f"RUN {entry['command']}")
    return "\n".join(lines) + "\n"


# ── the state machine ────────────────────────────────────────────────────────


def start_session(connection, settings: Any, *, project_id: str, actor: str,
                  base: Optional[str] = None, run: Optional[DockerRun] = None) -> dict:
    """Open a setup session: a row in `building/session` and its container."""
    require_docker_runtime(settings)
    base = (base or getattr(settings, "toolchain_default_base", "")
            or getattr(settings, "sandbox_container_image", "python:3.11-slim")).strip()
    if not base or any(ch.isspace() for ch in base):
        raise ToolchainImageError(f"{base!r} is not an image reference.")
    open_row = connection.execute(text(
        "SELECT id FROM toolchain_images WHERE project_id = :p AND status = 'building'"),
        {"p": project_id}).first()
    if open_row is not None:
        raise ToolchainImageError(
            f"this project already has a setup session open ({open_row[0]}). Capture or "
            "discard it first.")
    image_id = make_id("tci")
    network = getattr(settings, "toolchain_setup_network", "bridge") or "bridge"
    argv = [
        _binary(settings), "run", "--detach", "--init",
        "--label", "boss.owner=toolchain-setup",
        "--label", f"boss.toolchain_image={image_id}",
        "--label", f"boss.project_id={project_id}",
        "--name", f"boss-tcs-{image_id[-12:]}-{int(time.time())}",
        "--network", network,
        "--memory", str(getattr(settings, "sandbox_container_memory", "2g")),
        "--cpus", str(getattr(settings, "sandbox_container_cpus", "2")),
        base, "sleep", "infinity",
    ]
    try:
        proc = _docker(run)(argv, _SESSION_START_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise ToolchainImageError(
            f"starting the setup container from {base} timed out after "
            f"{_SESSION_START_TIMEOUT}s.") from exc
    if proc.returncode != 0:
        raise ToolchainImageError(f"could not start a setup container from {base}: {_out(proc)[:500]}")
    container_id = (proc.stdout or "").strip()
    connection.execute(text(
        "INSERT INTO toolchain_images (id, project_id, base, status, phase, container_id, "
        "created_by) VALUES (:id, :p, :base, 'building', 'session', :cid, :by)"),
        {"id": image_id, "p": project_id, "base": base, "cid": container_id, "by": actor})
    emit_event(connection, project_id, "toolchain_image.session_started", actor,
               "toolchain_image", image_id, {"base": base, "network": network})
    return get_image(connection, image_id)


def exec_in_session(connection, settings: Any, image_id: str, command: str, *, actor: str,
                    run: Optional[DockerRun] = None, timeout: Optional[int] = None) -> dict:
    """One terminal command in the open session (a shell line, as root). Audited."""
    require_docker_runtime(settings)
    row = get_image(connection, image_id)
    _require_open(row)
    command = (command or "").strip()
    if not command:
        raise ToolchainImageError("type a command to run.")
    if len(row["transcript"]) >= _MAX_TRANSCRIPT:
        raise ToolchainImageError(
            f"this session has run {_MAX_TRANSCRIPT} commands; capture it or start again.")
    limit = int(timeout or _EXEC_TIMEOUT)
    argv = [_binary(settings), "exec", "--env", "DEBIAN_FRONTEND=noninteractive",
            str(row["container_id"]), "sh", "-lc", command]
    started = time.monotonic()
    try:
        proc = _docker(run)(argv, limit)
        exit_code, stdout, stderr = proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        exit_code, stdout, stderr = 124, "", f"timed out after {limit}s"
    ms = int((time.monotonic() - started) * 1000)
    entry = {"command": command, "exit_code": exit_code, "duration_ms": ms, "actor": actor}
    _set(connection, image_id, transcript=[*row["transcript"], entry])
    emit_event(connection, row["project_id"], "toolchain_image.command", actor,
               "toolchain_image", image_id, {"command": command[:500], "exit_code": exit_code})
    return {**entry, "stdout": stdout[-_MAX_OUTPUT:], "stderr": stderr[-_MAX_OUTPUT:]}


def begin_capture(connection, settings: Any, image_id: str, *, actor: str) -> dict:
    """building/session -> building/capturing. Cheap: the slow part is finish_capture,
    which the API runs after answering so the console can show the chip at once."""
    require_docker_runtime(settings)
    row = get_image(connection, image_id)
    _require_open(row)
    _set(connection, image_id, phase=PHASE_CAPTURING,
         dockerfile=dockerfile_for(row["base"], row["transcript"]))
    emit_event(connection, row["project_id"], "toolchain_image.capture_started", actor,
               "toolchain_image", image_id, {"commands": len(row["transcript"])})
    return get_image(connection, image_id)


def finish_capture(connection, settings: Any, image_id: str, *, actor: str = "boss",
                   run: Optional[DockerRun] = None) -> dict:
    """building/capturing -> ready (commit [+ push], digest) or failed (error)."""
    row = get_image(connection, image_id)
    if row.get("status") != "building" or row.get("phase") != PHASE_CAPTURING:
        raise ToolchainImageError(f"toolchain image {image_id} is not being captured.")
    docker = _docker(run)
    binary = _binary(settings)
    container = str(row.get("container_id") or "")
    tag = f"{image_repository(settings, row['project_id'])}:{image_id.lower()}"
    try:
        commit = docker([binary, "commit",
                         "--change", "WORKDIR /work", "--change", 'CMD ["sleep", "infinity"]',
                         "--message", f"BOSS toolchain image {image_id}",
                         container, tag], _COMMIT_TIMEOUT)
        if commit.returncode != 0:
            raise ToolchainImageError(f"docker commit failed: {_out(commit)[:500]}")
        digest = (commit.stdout or "").strip().splitlines()[-1:] or [""]
        digest = digest[0].strip()
        pushed = False
        if registry(settings):
            push = docker([binary, "push", tag], _PUSH_TIMEOUT)
            if push.returncode != 0:
                raise ToolchainImageError(
                    f"pushing {tag} to {registry(settings)} failed: {_out(push)[:500]}")
            found = re.search(r"digest:\s*(sha256:[0-9a-f]{64})", _out(push))
            if not found:
                raise ToolchainImageError(f"the registry did not report a digest for {tag}.")
            digest, pushed = found.group(1), True
        if not digest.startswith("sha256:"):
            raise ToolchainImageError(f"docker commit did not return an image id ({digest!r}).")
    except (ToolchainImageError, subprocess.TimeoutExpired) as exc:
        message = (str(exc) if isinstance(exc, ToolchainImageError)
                   else f"capture timed out ({exc.timeout}s)")
        _remove_container(settings, container, run)
        _set(connection, image_id, status="failed", phase=PHASE_SESSION, container_id=None,
             error=message[:2000])
        emit_event(connection, row["project_id"], "toolchain_image.capture_failed", actor,
                   "toolchain_image", image_id, {"error": message[:500]})
        return get_image(connection, image_id)
    _remove_container(settings, container, run)
    image_ref = f"{tag.rsplit(':', 1)[0]}@{digest}" if pushed else tag
    _set(connection, image_id, status="ready", phase=PHASE_SESSION, container_id=None,
         image_ref=image_ref, digest=digest, error=None)
    emit_event(connection, row["project_id"], "toolchain_image.ready", actor,
               "toolchain_image", image_id, {"image_ref": image_ref, "digest": digest,
                                             "pushed": pushed})
    return get_image(connection, image_id)


def discard(connection, settings: Any, image_id: str, *, actor: str,
            run: Optional[DockerRun] = None) -> dict:
    """Close an open session without an image."""
    row = get_image(connection, image_id)
    _require_open(row)
    _remove_container(settings, str(row.get("container_id") or ""), run)
    _set(connection, image_id, status="failed", container_id=None,
         error=f"discarded by {actor}")
    emit_event(connection, row["project_id"], "toolchain_image.discarded", actor,
               "toolchain_image", image_id, {})
    return get_image(connection, image_id)


def _remove_container(settings: Any, container: str, run: Optional[DockerRun]) -> None:
    if not container:
        return
    try:
        _docker(run)([_binary(settings), "rm", "-f", container], 60)
    except Exception as exc:  # noqa: BLE001 — teardown never masks the real outcome
        _log.warning("could not remove toolchain setup container %s: %s", container[:12], exc)


# ── what a build runs ────────────────────────────────────────────────────────


def image_for_project(connection, project_id: Optional[str]) -> Optional[str]:
    """The image a docker-runtime build of this project runs, or None for the
    configured default. An execution environment's `config.image` (824) wins over
    the project's ready toolchain image: an operator who named an image on the
    environment the build resolves to meant it."""
    if not project_id:
        return None
    env = None
    try:
        from .execution_environments import resolve_environment

        # A SAVEPOINT of its own: on a database without the 824 tables the failed
        # SELECT must not abort the toolchain lookup that follows.
        with connection.begin_nested():
            env = resolve_environment(connection, project_id=project_id)
    except Exception:  # noqa: BLE001 — no environments (or a pre-824 DB): next source
        env = None
    if env:
        image = str((env.get("config") or {}).get("image") or "").strip()
        if image:
            return image
    row = ready_image(connection, project_id)
    return str(row["image_ref"]) if row else None


def runner_image_for(settings: Any, *, project_id: Optional[str] = None,
                     task_id: Optional[str] = None, engine: Any = None) -> Optional[str]:
    """ContainerCommandRunner's question, answered in its own short transaction.
    `task_id` is a code_task_runs id when the governed pipeline starts the runner
    (it passes the run id, not the project). Any failure — no database, a pre-825
    schema, an unknown run — is None: the default image, never a broken build."""
    if not project_id and not task_id:
        return None
    try:
        from ..db import begin, get_engine

        engine = engine or get_engine(settings)
        with begin(engine) as connection:
            # Each lookup in its own SAVEPOINT: a missing table must not abort the next.
            if not project_id:
                with connection.begin_nested():
                    found = connection.execute(
                        text("SELECT project_id FROM code_task_runs WHERE id = :id"),
                        {"id": task_id}).first()
                project_id = found[0] if found else None
            if not project_id:
                return None
            return image_for_project(connection, project_id)
    except Exception as exc:  # noqa: BLE001
        _log.debug("toolchain image lookup skipped (%s); using the default image", exc)
        return None
