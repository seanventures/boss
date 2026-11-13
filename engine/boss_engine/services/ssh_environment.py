"""The SSH execution environment driver (BOSSAPP-824 phase 1).

A Sandbox whose provision/run/diff/teardown execute on a remote host over SSH,
behind the SAME `Sandbox` protocol the governed pipeline already speaks
(sandbox_provider.Sandbox) — everything downstream is unchanged.

Deliberately reused rather than re-invented:
  * `HostAccess.run` + `host_access_for` (services/stack_deploy.py): one host,
    its user/port, and the host's OWN key resolved from
    deployment_hosts.key_secret_ref for this call only (ssh_host_adapter
    .stack_exec materialises it to a 0600 file and deletes it on return);
  * host-key pinning (services/host_key_pin.py): every remote call runs inside
    `pinned(engine, host_id)`, so the sandbox only ever talks to the machine
    that was verified;
  * the tar-over-stdin sync idiom (stack_deploy's upload step): sync_in pipes a
    tarball into `tar -xzf - -C <dir>`; sync_out and diff() come back as a
    base64'd tarball on stdout (stack_exec decodes stdout as text, so raw tar
    bytes would be mangled — base64 keeps them intact).

What this driver refuses, in words:
  * BOSS_SANDBOX_COMMAND_RUNTIME=docker — ContainerCommandRunner bind-mounts a
    LOCAL workspace path; combined with a remote sandbox it would silently run
    the agent's commands in a local docker against a path that does not exist
    here. SetupError, before anything is spent.
  * lineage warm-dir reuse — lineage_sandbox's warm checkouts live on the
    control-plane box (/var/lib/boss/lineage-sandboxes); a remote sandbox never
    claims one. warm_dir/reuse_warm are ignored (fresh remote clone) and said so.
  * a local (file://) repository — the remote host cannot read this machine's
    filesystem.

Scripts travel on STDIN (`sh -es`), never in argv: the clone credential —
egress's BOSS_EGR_CREDENTIAL or the token path's GIT_CONFIG_* extraheader —
must not appear in any process table, local or remote.
"""
from __future__ import annotations

import base64
import binascii
import gzip
import io
import logging
import shlex
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from ..schemas import FileChange
from .sandbox import CommandResult, DiffSummary, SandboxError, _looks_like_shell

_log = logging.getLogger(__name__)

#: Where remote sandboxes land unless the environment's config says otherwise.
DEFAULT_WORKROOT = "/var/tmp/boss-remote-sandboxes"

#: lineage_sandbox's warm checkouts are LOCAL-only; a remote driver never
#: touches a path under this root (the code-inventory gotcha).
LINEAGE_WARM_ROOT = "/var/lib/boss/lineage-sandboxes"


def _setup_error(message: str) -> Exception:
    from .setup_service import SetupError

    return SetupError(message)


def _refuse_docker_runtime(settings: Any) -> None:
    runtime = str(getattr(settings, "sandbox_command_runtime", "subprocess") or "").lower()
    if runtime == "docker":
        raise _setup_error(
            "BOSS_SANDBOX_COMMAND_RUNTIME=docker cannot be combined with a remote "
            "(ssh) execution environment: the docker command runtime bind-mounts a "
            "workspace path on the CONTROL-PLANE box, and the workspace lives on "
            "the remote host. Set BOSS_SANDBOX_COMMAND_RUNTIME=subprocess for this "
            "environment, or build on the local substrate."
        )


def _host_row(connection, settings: Any, environment: dict) -> dict:
    """The machine this environment runs on: config.host_id names a
    deployment_hosts row (whose key + pinned host key are reused), or the
    config carries the address inline with secret_refs.key_secret_ref."""
    config = environment.get("config") or {}
    refs = environment.get("secret_refs") or {}
    host_id = str(config.get("host_id") or "").strip()
    if host_id:
        from .deployment_fleet import get_host

        host = get_host(connection, host_id)
        if not host:
            raise _setup_error(
                f"environment {environment.get('name')!r} names host {host_id!r}, "
                "which does not exist — pick a host in Infra, or fix the "
                "environment's config.host_id."
            )
        return host
    address = str(config.get("address") or "").strip()
    if not address:
        raise _setup_error(
            f"environment {environment.get('name')!r} has no machine: set "
            "config.host_id to a deployment host, or config.address plus "
            "secret_refs.key_secret_ref."
        )
    return {
        "id": None,  # no deployment_hosts row — no stored host-key pin to enforce
        "name": environment.get("name"),
        "address": address,
        "ssh_user": config.get("ssh_user") or "root",
        "ssh_port": config.get("ssh_port") or 22,
        "key_secret_ref": refs.get("key_secret_ref") or config.get("key_secret_ref"),
    }


def _access_for(connection, settings: Any, host: dict, adapter: Any = None):
    from .stack_deploy import StackDeployError, host_access_for

    try:
        return host_access_for(connection, settings, host, adapter)
    except StackDeployError as exc:
        raise _setup_error(str(exc)) from exc


class SSHEnvironmentDriver:
    """EnvironmentDriver for `driver='ssh'` rows: acquire() -> SSHSandbox,
    probe() -> the console Test button's health dict, release() -> teardown."""

    kind = "ssh"

    def __init__(self, settings: Any, *, engine: Any, environment: dict,
                 adapter: Any = None):
        self.settings = settings
        self.engine = engine
        self.environment = environment
        self._adapter = adapter  # tests substitute a recorded fake transport

    def _begin(self):
        from ..db import begin

        return begin(self.engine)

    def acquire(self, *, task_id: str, tenant_id: Optional[str] = None) -> "SSHSandbox":
        _refuse_docker_runtime(self.settings)
        from . import execution_environments as envs

        with self._begin() as connection:
            host = _host_row(connection, self.settings, self.environment)
            access = _access_for(connection, self.settings, host, self._adapter)
            lease = envs.acquire_lease(
                connection,
                environment_id=str(self.environment.get("id") or ""),
                run_id=task_id,
                fingerprint=envs.lease_fingerprint(self.environment),
                ttl_seconds=(self.environment.get("config") or {}).get("lease_ttl_seconds"),
            )
        return SSHSandbox(
            self.settings,
            task_id=task_id,
            access=access,
            engine=self.engine,
            host_id=host.get("id"),
            environment=self.environment,
            lease_id=lease["id"],
        )

    def probe(self) -> dict:
        try:
            with self._begin() as connection:
                return probe_over_ssh(connection, self.settings, self.environment,
                                      adapter=self._adapter)
        except Exception as exc:  # noqa: BLE001 — a probe answers, it never crashes
            return {"ok": False, "driver": "ssh", "error": f"{exc}"[:400]}

    def release(self, sandbox: Any) -> None:
        try:
            sandbox.teardown()
        except Exception:  # noqa: BLE001 — release is best-effort; the reaper backstops
            _log.warning("ssh environment release failed", exc_info=True)


def probe_over_ssh(connection, settings: Any, environment: dict,
                   adapter: Any = None) -> dict:
    """Actually open a session and ask for what a build needs: reachability,
    git, the workroot's writability, and the architecture. Read-only."""
    from . import host_key_pin

    host = _host_row(connection, settings, environment)
    access = _access_for(connection, settings, host, adapter)
    workroot = _workroot(environment)
    script = (
        "echo BOSS_SSH_OK; "
        "command -v git >/dev/null && echo GIT_OK || echo GIT_MISSING; "
        f"mkdir -p {shlex.quote(workroot)} 2>/dev/null && "
        f"test -w {shlex.quote(workroot)} && echo WORKROOT_OK || echo WORKROOT_RO; "
        "uname -m"
    )
    with host_key_pin.pinned(connection, host.get("id")):
        result = access.run(script, timeout=25)
        refused = host_key_pin.note_result(result.returncode, result.stderr)
    out = f"{result.stdout or ''}{result.stderr or ''}"
    if refused:
        return {"ok": False, "driver": "ssh", "error": refused}
    if "BOSS_SSH_OK" not in out:
        detail = (result.stderr or result.stdout or "").strip()
        return {"ok": False, "driver": "ssh",
                "error": f"ssh to {host.get('address')} failed: {detail[:300]}"}
    probe: dict[str, Any] = {
        "ok": "GIT_OK" in out and "WORKROOT_OK" in out,
        "driver": "ssh",
        "reachable": True,
        "git": "GIT_OK" in out,
        "workroot": workroot,
        "workroot_writable": "WORKROOT_OK" in out,
    }
    for line in out.splitlines():
        token = line.strip()
        if token in ("x86_64", "amd64", "aarch64", "arm64"):
            probe["arch"] = "amd64" if token in ("x86_64", "amd64") else "arm64"
    if not probe["git"]:
        probe["error"] = "git is not installed on the host."
    elif not probe["workroot_writable"]:
        probe["error"] = f"{workroot} is not writable by {host.get('ssh_user') or 'root'}."
    return probe


def _workroot(environment: dict) -> str:
    config = environment.get("config") or {}
    return str(config.get("workdir_root") or DEFAULT_WORKROOT).rstrip("/") or DEFAULT_WORKROOT


class SSHSandbox:
    """One task's working tree on a remote host — the `Sandbox` protocol over
    `HostAccess.run`. `path` is the REMOTE workdir (a PurePosixPath): local
    file reads against it are a bug, which is exactly why the docker command
    runtime is refused up front."""

    def __init__(self, settings: Any, *, task_id: str, access: Any,
                 engine: Any = None, host_id: Optional[str] = None,
                 environment: Optional[dict] = None, lease_id: Optional[str] = None):
        self.settings = settings
        self.task_id = task_id
        self.access = access
        self.engine = engine
        self.host_id = host_id
        self.environment = environment or {}
        self.lease_id = lease_id
        self.workroot = _workroot(self.environment)
        self.workdir = f"{self.workroot}/{task_id}"
        self.path: Optional[PurePosixPath] = None
        self.base_branch: Optional[str] = None
        self.work_branch: Optional[str] = None
        self.base_sha: Optional[str] = None
        self.diff_base_sha: Optional[str] = None
        self.resumed_from_checkpoint = False
        self.warm_reused = False               # never: warm reuse is local-only
        self.integration_ref = None
        self.integration_fetch_error = None
        self.unreadable_paths: list[str] = []
        self._repo_files = None                # local-only concept; stays None

    # -- lifecycle ------------------------------------------------------------
    def __enter__(self) -> "SSHSandbox":
        return self

    def __exit__(self, *exc) -> None:
        self.teardown()

    # -- transport ------------------------------------------------------------
    def _remote(self, command: str, *, timeout: int, stdin_text: Optional[str] = None,
                stdin_path: Optional[str] = None):
        """One remote call, under the host's pinned key. A key mismatch is a
        refusal in words, never a confusing git error."""
        from . import host_key_pin

        pin_source = self.engine if (self.engine is not None and self.host_id) else None
        if pin_source is not None:
            context = host_key_pin.pinned(pin_source, self.host_id)
        else:
            import contextlib

            context = contextlib.nullcontext()
        with context:
            result = self.access.run(command, timeout=timeout,
                                     stdin_text=stdin_text, stdin_path=stdin_path)
            refused = host_key_pin.note_result(result.returncode, result.stderr)
        if refused:
            raise SandboxError(refused)
        return result

    def _script(self, script: str, *, timeout: int, stdin_path: Optional[str] = None):
        """Run a script with `sh -es`, the script on stdin — a credential in it
        never reaches argv or a process table. With a binary payload to feed
        (sync_in), the command itself must be plain and stdin carries the data."""
        if stdin_path is not None:
            return self._remote(script, timeout=timeout, stdin_path=stdin_path)
        return self._remote("sh -es", timeout=timeout, stdin_text=script)

    # -- provision ------------------------------------------------------------
    def provision(
        self, owner: str, repo: str, *, base_branch: str = "main",
        work_branch: Optional[str] = None, token: Optional[str] = None,
        repo_host: str = "github", egress: Optional[Any] = None,
        warm_dir: Optional[Any] = None, reuse_warm: bool = False,
    ) -> PurePosixPath:
        """Create the remote workdir and clone exactly what EphemeralSandbox
        clones locally — same credential rules (egress first, the token path's
        env-borne extraheader otherwise; nothing on disk or argv), same work
        branch resume semantics. Lineage warm dirs are local-only and are never
        claimed here."""
        if self.path is not None:
            raise SandboxError("Sandbox already provisioned.")
        _refuse_docker_runtime(self.settings)
        if warm_dir is not None or reuse_warm:
            _log.info(
                "ssh sandbox task=%s: lineage warm reuse is local-only; "
                "provisioning a fresh remote clone instead", self.task_id)
        self.base_branch = base_branch
        self.work_branch = work_branch or f"boss/{self.task_id}"

        clone_url, config_pairs, cred_env = self._clone_access(
            owner, repo, token=token, repo_host=repo_host, egress=egress)
        if clone_url.startswith("file://"):
            raise _setup_error(
                f"{owner}/{repo} is a local repository on the control-plane box; "
                "a remote (ssh) environment cannot reach it. Build it on the "
                "local substrate, or publish it to GitHub first."
            )

        depth = max(1, int(getattr(self.settings, "sandbox_clone_depth", 1) or 1))
        q = shlex.quote
        config_args = " ".join(f"-c {q(f'{k}={v}')}" for k, v in config_pairs)
        exports = "\n".join(
            f"export {k}={q(str(v))}" for k, v in {**cred_env,
                                                   "GIT_TERMINAL_PROMPT": "0"}.items()
        )
        wd, wb, bb = q(self.workdir), q(self.work_branch), q(base_branch)
        git = f"git {config_args}".strip()
        script = f"""set -e
umask 077
mkdir -p {q(self.workroot)}
rm -rf {wd}
{exports}
{git} clone --depth {depth} --branch {bb} {q(clone_url)} {wd}
cd {wd}
git config user.name boss-agent
git config user.email boss-agent@users.noreply.github.com
if [ {wb} != {bb} ]; then
  if git ls-remote --exit-code --heads origin {wb} >/dev/null 2>&1; then
    git fetch --depth {depth} origin {q(self.work_branch + ':refs/remotes/origin/' + self.work_branch)}
    git checkout -b {wb} {q('refs/remotes/origin/' + self.work_branch)}
    echo BOSS_RESUMED
  else
    git checkout -b {wb}
  fi
fi
echo BOSS_SHA=$(git rev-parse HEAD)
echo BOSS_PROVISIONED
"""
        result = self._script(script, timeout=900)
        out = f"{result.stdout or ''}"
        if result.returncode != 0 or "BOSS_PROVISIONED" not in out:
            detail = (result.stderr or result.stdout or "").strip()
            raise SandboxError(
                f"remote provision of {owner}/{repo} failed on "
                f"{self.access.host}: {detail[-600:]}"
            )
        for line in out.splitlines():
            if line.startswith("BOSS_SHA="):
                self.base_sha = line.split("=", 1)[1].strip() or None
        self.diff_base_sha = self.base_sha
        self.resumed_from_checkpoint = "BOSS_RESUMED" in out
        self.path = PurePosixPath(self.workdir)
        return self.path

    def _clone_access(self, owner: str, repo: str, *, token: Optional[str],
                      repo_host: str, egress: Optional[Any]):
        """(url, git -c pairs, credential env) — mirrors EphemeralSandbox._provision:
        egress wins (credential-free URL + the own-git helper fed by
        BOSS_EGR_CREDENTIAL in the environment), else repo_host.git_access's
        env-borne extraheader. Neither form writes a secret to the checkout."""
        if egress is not None:
            pairs = [tuple(kv) for kv in egress.git_config
                     if kv[0] != "credential.helper"]
            env: dict[str, str] = {}
            own = getattr(egress, "own_git", None)
            if own is not None:
                pairs += [tuple(kv) for kv in own.config]
                env = dict(own.env)
            return egress.clone_url, pairs, env
        from .repo_host import git_access

        access = git_access(repo_host, owner, repo, token or "")
        return access.url, [], dict(access.env)

    # -- commands -------------------------------------------------------------
    def run(self, command: str, *, timeout: Optional[int] = None,
            cwd: Optional[str] = None, env: Optional[dict] = None) -> CommandResult:
        """One build/test command in the remote workdir. The same contract as
        EphemeralSandbox.run: shell syntax is refused in the same words (the
        remote shell WOULD interpret it, so allowing it here would make the two
        substrates behave differently), and credential-shaped env names are
        dropped."""
        self._require_provisioned()
        argv = shlex.split(command)
        if not argv:
            raise SandboxError("Empty command.")
        if _looks_like_shell(command):
            raise SandboxError(
                f"{command!r} uses shell syntax, and sandbox commands run WITHOUT a shell. "
                "Operators like && || | ; > $ ( ) are not interpreted — the command would "
                "run only up to the first one. Give one plain command, or put the sequence "
                "in a script the repo already has (e.g. an npm script).")
        from .agent_env import SECRET_SHAPED

        q = shlex.quote
        extra = {k: v for k, v in (env or {}).items()
                 if not any(m in k.upper() for m in SECRET_SHAPED)}
        exports = "\n".join(f"export {k}={q(str(v))}" for k, v in extra.items())
        where = self.workdir if not cwd else f"{self.workdir}/{cwd}"
        limit = int(timeout or getattr(self.settings,
                                       "sandbox_validation_timeout_seconds", 600) or 600)
        script = f"set -e\ncd {q(where)}\n{exports}\n{command}\n"
        started = time.monotonic()
        result = self._script(script, timeout=limit)
        duration_ms = int((time.monotonic() - started) * 1000)
        timed_out = result.returncode == 124
        return CommandResult(
            command=command, argv=argv, exit_code=result.returncode,
            duration_ms=duration_ms, timed_out=timed_out,
            stdout=result.stdout or "", stderr=result.stderr or "",
        )

    # -- diff -----------------------------------------------------------------
    def diff_summary(self) -> DiffSummary:
        self._require_provisioned()
        base = self.diff_base_sha or ""
        q = shlex.quote
        script = (f"set -e\ncd {q(self.workdir)}\ngit add -A\n"
                  f"git diff --cached --numstat {q(base) if base else ''}\n")
        result = self._script(script, timeout=300)
        if result.returncode != 0:
            raise SandboxError(f"remote diff failed: {(result.stderr or '')[-400:]}")
        additions = deletions = 0
        paths: list[str] = []
        for line in (result.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            add, dele, path = parts
            additions += 0 if add == "-" else int(add)
            deletions += 0 if dele == "-" else int(dele)
            paths.append(path)
        return DiffSummary(files_changed=len(paths), additions=additions,
                           deletions=deletions, paths=paths)

    def unified_diff(self) -> str:
        self._require_provisioned()
        base = self.diff_base_sha or ""
        q = shlex.quote
        script = (f"set -e\ncd {q(self.workdir)}\ngit add -A\n"
                  f"git diff --cached {q(base) if base else ''}\n")
        result = self._script(script, timeout=300)
        if result.returncode != 0:
            raise SandboxError(f"remote diff failed: {(result.stderr or '')[-400:]}")
        return result.stdout or ""

    def diff(self) -> list[FileChange]:
        """The changed files' CONTENT, fetched as one base64'd tarball (one
        round trip, binary-safe over the text transport), then filtered exactly
        as the local sandbox filters: build artifacts out, binary files out."""
        self._require_provisioned()
        summary = self.diff_summary()
        if not summary.paths:
            return []
        from .governed_code_task_service import workspace_keepable

        q = shlex.quote
        base = self.diff_base_sha or ""
        listing = f"/tmp/boss-diff-{self.task_id}.lst"
        script = (
            f"set -e\ncd {q(self.workdir)}\n"
            f"git diff --cached --name-only -z {q(base) if base else ''} > {q(listing)}\n"
            f"tar -czf - --null --ignore-failed-read -T {q(listing)} 2>/dev/null | base64\n"
            f"rm -f {q(listing)}\n"
        )
        result = self._script(script, timeout=600)
        if result.returncode != 0:
            raise SandboxError(f"remote diff export failed: {(result.stderr or '')[-400:]}")
        try:
            blob = base64.b64decode("".join((result.stdout or "").split()))
        except (binascii.Error, ValueError) as exc:
            raise SandboxError(f"remote diff export was not base64: {exc}") from exc
        contents: dict[str, str] = {}
        self.unreadable_paths = []
        if blob:
            with tarfile.open(fileobj=io.BytesIO(gzip.decompress(blob)), mode="r:") as archive:
                for member in archive:
                    if not member.isfile():
                        continue
                    name = member.name.lstrip("./")
                    handle = archive.extractfile(member)
                    if handle is None:
                        continue
                    try:
                        contents[name] = handle.read().decode("utf-8", errors="strict")
                    except UnicodeDecodeError:
                        continue  # binary — same local-sandbox rule
        changes: list[FileChange] = []
        for path in summary.paths:
            if not workspace_keepable(path):
                continue
            if path in contents:
                changes.append(FileChange(path=path, content=contents[path]))
            # absent from the tarball: deleted file — skipped, as locally
        return changes

    # -- sync (the tar idiom) --------------------------------------------------
    def sync_in(self, local_path: Any, remote_rel: str = "") -> None:
        """Pipe a local directory into the remote workdir as a tarball on stdin
        — the exact upload idiom stack_deploy uses (`tar -xzf - -C <dir>`)."""
        self._require_provisioned()
        source = Path(local_path)
        if not source.is_dir():
            raise SandboxError(f"sync_in needs a directory, got {source}")
        q = shlex.quote
        dest = self.workdir if not remote_rel else f"{self.workdir}/{remote_rel}"
        fd_path = tempfile.mkstemp(suffix=".tar.gz")[1]
        try:
            with tarfile.open(fd_path, "w:gz") as archive:
                archive.add(source, arcname=".")
            result = self._remote(
                f"mkdir -p {q(dest)} && tar -xzf - -C {q(dest)}",
                timeout=900, stdin_path=fd_path)
        finally:
            try:
                Path(fd_path).unlink()
            except OSError:
                pass
        if result.returncode != 0:
            raise SandboxError(f"sync_in failed: {(result.stderr or '')[-400:]}")

    def sync_out(self, remote_rel: str, local_path: Any) -> None:
        """Bring a remote directory back: tar on the host, base64 over stdout,
        safe-extracted locally (no absolute members, no path escapes)."""
        self._require_provisioned()
        q = shlex.quote
        source = self.workdir if not remote_rel else f"{self.workdir}/{remote_rel}"
        result = self._script(
            f"set -e\ncd {q(source)}\ntar -czf - . | base64\n", timeout=900)
        if result.returncode != 0:
            raise SandboxError(f"sync_out failed: {(result.stderr or '')[-400:]}")
        try:
            blob = base64.b64decode("".join((result.stdout or "").split()))
        except (binascii.Error, ValueError) as exc:
            raise SandboxError(f"sync_out was not base64: {exc}") from exc
        destination = Path(local_path)
        destination.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
            for member in archive:
                name = member.name.lstrip("./")
                if not name or name.startswith("/") or ".." in PurePosixPath(name).parts:
                    continue  # never extract outside the destination
                if member.issym() or member.islnk():
                    continue
                if member.isdir():
                    (destination / name).mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    continue
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                handle = archive.extractfile(member)
                if handle is not None:
                    target.write_bytes(handle.read())

    # -- teardown --------------------------------------------------------------
    def teardown(self) -> None:
        """rm the remote workdir and release the lease. Never touches a lineage
        warm path — those are local-only and owned by lineage_sandbox.purge."""
        if self.path is not None:
            workdir = str(self.path)
            if (workdir.startswith(f"{self.workroot}/")
                    and not workdir.startswith(LINEAGE_WARM_ROOT)):
                try:
                    self._script(f"rm -rf {shlex.quote(workdir)}\n", timeout=120)
                except Exception:  # noqa: BLE001 — best effort; the reaper's TTL backstops
                    _log.warning("ssh sandbox teardown of %s failed", workdir,
                                 exc_info=True)
            self.path = None
        self._release_lease()

    def _release_lease(self) -> None:
        if not self.lease_id or self.engine is None:
            return
        try:
            from ..db import begin
            from . import execution_environments as envs

            with begin(self.engine) as connection:
                envs.release_lease(connection, lease_id=self.lease_id)
        except Exception:  # noqa: BLE001 — the reaper expires what this misses
            _log.warning("ssh sandbox lease release failed", exc_info=True)
        self.lease_id = None

    # -- internals --------------------------------------------------------------
    def _require_provisioned(self) -> None:
        if self.path is None:
            raise SandboxError("Sandbox is not provisioned.")
