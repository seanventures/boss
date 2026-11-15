"""`local_fenced`: an authored command runs in the worker's own per-run fence (AMEX-G2, BOSSAPP-1138).

`skill.sandboxed_command` refused to run anywhere but a microVM substrate, because the `local`
provider is a subprocess as the control plane's own uid: remote code execution against the box
that holds every project's credentials. A `runtime: local` install has no microVM, so the AMEX
onboarding workflow could not run `gwlint validate tenant.yaml` at all.

The worker already has a fence every code-task run stands in (sandbox_identity, IDENTITY.md
§7.6): a pool uid leased for the run, `boss-sandbox-exec` (new session, that uid, no groups,
no_new_privs), a run directory only that uid and BOSS can enter, and the entrypoint's iptables
owner-match rules — the pool reaches loopback, the API and boss-egress's relays ONLY; the
database, the host, the internet are rejected. This module puts one skill command inside it:

  * a fresh owner (`skill_run:<task>`) leases its own uid and directory for the call;
  * the skill's `files` inputs are written into a workdir there (the YAML a step hands on);
  * the command runs through owned_process → boss-sandbox-exec as that uid, cwd = the workdir,
    the agent allow-list environment (no DATABASE_URL, no provider keys), the egress CONNECT
    proxy for anything that is not loopback, a bounded timeout;
  * stdout/stderr are capped; the declared outputs it produced are kept as artifacts so the next
    step (deploy_config) can ship them after this directory is gone;
  * at teardown every process of the uid is killed and the directory removed.

WHAT IT IS NOT. A pool uid behind iptables shares the control plane's kernel. It isolates a
command from other runs and from BOSS's secrets; it is not a separate machine. So every answer
says `isolation: {"provider": "local_fenced", "strong": False}`, a2a_server.strong_isolation()
stays False, and an install marked BOSS_DEPLOYMENT_TRUST=untrusted refuses it outright.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import logging
import os
import posixpath
import subprocess
from types import SimpleNamespace
from typing import Any, Mapping, Optional

log = logging.getLogger(__name__)

PROVIDER = "local_fenced"
#: Bytes of stdout (and of stderr) kept from one command. The executor hands back the tail.
OUTPUT_CAP = 64_000
#: Bytes of one produced file kept as an artifact. A tenant YAML is kilobytes; a video is not
#: what this path is for.
PRODUCED_CAP = 1_000_000
#: One input file's size cap, for the same reason.
INPUT_CAP = 2_000_000
OWNER_KIND = "skill_run"
ARTIFACT_TYPE = "produced_file"


class LocalFenceUnavailable(RuntimeError):
    """The worker has no fence to run this in. Said in the words of the one thing to fix."""


def fence_problem(settings: Any) -> Optional[str]:
    """Why `local_fenced` cannot run here, or None when it can.

    Refuses, in order: an install marked untrusted (strong isolation only); the fence switched
    off (BOSS_SANDBOX_ISOLATION=off — nothing isolates); the helper, roots or network rules
    missing (sandbox_identity.unavailable_reason says which)."""
    from . import sandbox_identity

    trust = str(getattr(settings, "deployment_trust", "") or "trusted").strip().lower()
    if trust == "untrusted":
        return ("this deployment is marked untrusted (BOSS_DEPLOYMENT_TRUST=untrusted): an "
                "authored command runs only under strong isolation — set "
                "BOSS_GOVERNED_SANDBOX_PROVIDER to a microVM substrate.")
    if sandbox_identity.mode() == "off":
        return ("the worker's sandbox fence is switched off (BOSS_SANDBOX_ISOLATION=off), so "
                "there is nowhere fenced to run an authored command. Set it to required (the "
                "container default) or use a microVM substrate.")
    if not sandbox_identity.available():
        return sandbox_identity.unavailable_reason()
    return None


def safe_relative_path(name: Any) -> str:
    """A file name a skill or a step hands in, confined to the workdir: relative, normalised,
    no `..`, no leading slash, no empty segment. Raises ValueError otherwise."""
    raw = str(name or "").strip().replace("\\", "/")
    if not raw:
        raise ValueError("a file needs a name")
    norm = posixpath.normpath(raw)
    if norm.startswith("/") or norm == "." or norm.startswith("../") or norm == ".." \
            or any(part in ("", ".", "..") for part in norm.split("/")):
        raise ValueError(f"file name {raw!r} must be a relative path inside the workdir")
    if len(norm) > 240:
        raise ValueError(f"file name {raw!r} is too long")
    return norm


def file_bytes(value: Any) -> bytes:
    """The bytes of one `files` entry: a string (text), or {"text"}, {"base64"}, {"content"}."""
    if isinstance(value, bytes):
        data = value
    elif isinstance(value, str):
        data = value.encode("utf-8")
    elif isinstance(value, Mapping):
        if value.get("base64") is not None:
            data = base64.b64decode(str(value["base64"]))
        else:
            text = value.get("text", value.get("content"))
            if text is None:
                raise ValueError("a file entry needs text, content or base64")
            data = str(text).encode("utf-8")
    else:
        raise ValueError("a file entry is a string or {text|content|base64}")
    if len(data) > INPUT_CAP:
        raise ValueError(f"a file input is capped at {INPUT_CAP} bytes")
    return data


def _cap(data: bytes, cap: Optional[int] = None) -> tuple[str, bool]:
    cap = OUTPUT_CAP if cap is None else int(cap)
    truncated = len(data) > cap
    kept = data[-cap:] if truncated else data
    return kept.decode("utf-8", "replace"), truncated


class LocalFencedSandbox:
    """One skill command's fenced home: its own uid, its own workdir, gone at teardown.

    Mirrors the `acquire → run → teardown` surface the executor used with the microVM provider,
    so `execute_sandboxed_command` reads the same result either way."""

    def __init__(self, settings: Any, *, task_id: str, files: Optional[Mapping[str, Any]] = None,
                 project_id: Optional[str] = None):
        from . import owned_process, sandbox_identity

        problem = fence_problem(settings)
        if problem:
            raise LocalFenceUnavailable(problem)
        self.settings = settings
        self.task_id = task_id
        self.owner_id = f"skill:{task_id}"
        self.project_id = project_id
        self._owned_process = owned_process
        self._identity_module = sandbox_identity
        self.identity = sandbox_identity.acquire(self.owner_id, what="skill")
        if self.identity is None:
            # `auto` with no helper: acquire says so loudly and returns None. That is an
            # unfenced run — exactly what this provider exists not to do.
            raise LocalFenceUnavailable(sandbox_identity.unavailable_reason())
        try:
            self.workdir = self.identity.workspace("skill-")
            self.inputs: dict[str, str] = {}
            for name, value in (files or {}).items():
                rel = safe_relative_path(name)
                path = self.workdir / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                with contextlib.suppress(OSError):
                    os.chmod(path.parent, 0o2777)
                path.write_bytes(file_bytes(value))
                os.chmod(path, 0o644)
                self.inputs[str(name)] = str(path)
        except BaseException:
            self.teardown()
            raise

    # ---- running -----------------------------------------------------------------------------
    def run(self, command: str, *, timeout: int = 300) -> Any:
        """Run `command` through /bin/sh as the leased uid, cwd = the workdir. Returns an object
        with exit_code, stdout, stderr (tails, capped), timed_out and truncated."""
        timeout = max(1, int(timeout or 300))
        timed_out = False
        with self._owned_process.owner_scope(OWNER_KIND, self.owner_id, project_id=self.project_id):
            try:
                proc = self._owned_process.run(
                    ["/bin/sh", "-c", command], cwd=str(self.workdir), capture_output=True,
                    timeout=timeout, env=None)
                code, out, err = proc.returncode, proc.stdout or b"", proc.stderr or b""
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                code = 124
                out = exc.stdout or b""
                err = (exc.stderr or b"") + f"\nboss: command timed out after {timeout}s".encode()
        if isinstance(out, str):
            out = out.encode("utf-8", "replace")
        if isinstance(err, str):
            err = err.encode("utf-8", "replace")
        stdout, cut_out = _cap(out)
        stderr, cut_err = _cap(err)
        return SimpleNamespace(exit_code=code, stdout=stdout, stderr=stderr, timed_out=timed_out,
                               truncated=cut_out or cut_err, workdir=str(self.workdir))

    # ---- what it produced --------------------------------------------------------------------
    def produced_files(self, outputs: Any) -> dict[str, dict[str, Any]]:
        """The declared outputs that exist in the workdir after the command: name → {path,
        sha256, size, bytes}. A declared output the command did not write is simply absent."""
        found: dict[str, dict[str, Any]] = {}
        for entry in outputs or []:
            name = str(entry.get("name") or "").strip() if isinstance(entry, Mapping) else str(entry)
            if not name:
                continue
            rel = str((entry.get("path") if isinstance(entry, Mapping) else None) or name)
            try:
                rel = safe_relative_path(rel)
            except ValueError:
                continue
            path = self.workdir / rel
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
                if size > PRODUCED_CAP:
                    log.warning("produced output %s is %d bytes; capped, not kept", rel, size)
                    continue
                data = path.read_bytes()
            except OSError:
                continue
            found[name] = {"path": rel, "size": len(data),
                           "sha256": hashlib.sha256(data).hexdigest(), "bytes": data}
        return found

    def teardown(self) -> None:
        with contextlib.suppress(Exception):
            self._identity_module.release(self.owner_id)


def keep_produced(connection: Any, settings: Any, *, project_id: str, skill_key: str,
                  produced: Mapping[str, Mapping[str, Any]],
                  created_by_job_id: Optional[str] = None) -> dict[str, str]:
    """Store each produced file as an artifact (`produced_file`) so a later step can deploy
    exactly these bytes after the fenced directory is gone. Returns name → artifact id."""
    from .core import create_artifact

    out: dict[str, str] = {}
    for name, item in produced.items():
        data: bytes = item["bytes"]
        try:
            text = data.decode("utf-8")
            body = {"text": text}
        except UnicodeDecodeError:
            body = {"base64": base64.b64encode(data).decode("ascii")}
        out[name] = create_artifact(
            connection, settings, project_id, None, ARTIFACT_TYPE,
            f"{skill_key}: {item['path']}",
            {"name": name, "path": item["path"], "size": item["size"], "sha256": item["sha256"],
             "skill_key": skill_key, "provider": PROVIDER, **body},
            created_by_job_id=created_by_job_id)
    return out
