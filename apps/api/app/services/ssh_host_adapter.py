from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass

from ..config import Settings
from .github_adapter import IntegrationConfigurationError


@dataclass
class WorkloadDeployResult:
    host: str
    workload: str
    image: str
    container_id: str
    status: str
    ports: list[str]


@dataclass
class WorkloadHealthResult:
    host: str
    workload: str
    running: bool
    status: str


@dataclass
class WorkloadTeardownResult:
    host: str
    workload: str
    removed: bool


@dataclass
class HostConnectionResult:
    host: str
    reachable: bool
    docker_version: str


class SSHHostAdapter:
    """Deploy Docker workloads onto a customer-provided host over SSH.

    Path (i) of the external-delivery plan (docs/product/EXTERNAL_CHATBOT_DELIVERY_GAP.md):
    the customer hands BOSS a running host (address + SSH private key). BOSS launches
    single containers (Neo4j, ingestion worker, chatbot) via ``docker run``. The private
    key is a SecretRef written to a 0600 temp file only for the duration of a call and
    never enters agent context.
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    # -- credential / gate -------------------------------------------------
    def _guard(self) -> str:
        if not self.settings.ssh_host_deploy_allowed:
            raise IntegrationConfigurationError(
                "BOSS_SSH_HOST_DEPLOY_ALLOWED is false."
            )
        key = self.settings.ssh_host_private_key
        if not key:
            raise IntegrationConfigurationError(
                "BOSS_SSH_HOST_PRIVATE_KEY is not configured."
            )
        return key

    def _run_remote(
        self,
        *,
        host: str,
        user: str,
        port: int,
        command: str,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess:
        key = self._guard()
        fd, key_path = tempfile.mkstemp(suffix=".pem")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(key if key.endswith("\n") else key + "\n")
            os.chmod(key_path, 0o600)
            argv = [
                "ssh",
                "-i",
                key_path,
                "-p",
                str(port),
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                "ConnectTimeout=15",
                f"{user}@{host}",
                command,
            ]
            return subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout
            )
        finally:
            try:
                os.unlink(key_path)
            except OSError:
                pass

    # -- capabilities ------------------------------------------------------
    def connection_test(
        self, *, host: str, user: str = "root", port: int = 22
    ) -> HostConnectionResult:
        proc = self._run_remote(
            host=host, user=user, port=port, command="docker version --format '{{.Server.Version}}'"
        )
        reachable = proc.returncode == 0
        return HostConnectionResult(
            host=host,
            reachable=reachable,
            docker_version=(proc.stdout or "").strip() if reachable else "",
        )

    def deploy_workload(
        self,
        *,
        host: str,
        workload: str,
        image: str,
        user: str = "root",
        port: int = 22,
        env: dict[str, str] | None = None,
        ports: list[str] | None = None,
        volumes: list[str] | None = None,
        command: str | None = None,
    ) -> WorkloadDeployResult:
        """Idempotent single-container deploy: remove any prior container with the
        same name, then ``docker run -d`` the new one. Env values are passed via
        the remote shell with shlex quoting; secrets should arrive as SecretRef
        resolutions, never logged."""
        env = env or {}
        ports = ports or []
        volumes = volumes or []
        name = shlex.quote(workload)
        parts = ["docker", "rm", "-f", name, ">/dev/null", "2>&1", ";", "docker", "run", "-d",
                 "--name", name, "--restart", "unless-stopped"]
        for key, value in env.items():
            parts += ["-e", shlex.quote(f"{key}={value}")]
        for mapping in ports:
            parts += ["-p", shlex.quote(mapping)]
        for mapping in volumes:
            parts += ["-v", shlex.quote(mapping)]
        parts.append(shlex.quote(image))
        if command:
            parts.append(command)
        proc = self._run_remote(
            host=host, user=user, port=port, command=" ".join(parts), timeout=300
        )
        if proc.returncode != 0:
            raise IntegrationConfigurationError(
                f"ssh_host deploy failed on {host}: {(proc.stderr or proc.stdout or '').strip()[:500]}"
            )
        container_id = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""
        return WorkloadDeployResult(
            host=host,
            workload=workload,
            image=image,
            container_id=container_id,
            status="running",
            ports=list(ports),
        )

    def health_check(
        self, *, host: str, workload: str, user: str = "root", port: int = 22
    ) -> WorkloadHealthResult:
        name = shlex.quote(workload)
        proc = self._run_remote(
            host=host,
            user=user,
            port=port,
            command=f"docker inspect -f '{{{{.State.Running}}}} {{{{.State.Status}}}}' {name}",
        )
        out = (proc.stdout or "").strip()
        running = proc.returncode == 0 and out.lower().startswith("true")
        status = out.split(" ", 1)[1] if " " in out else (out or "unknown")
        return WorkloadHealthResult(
            host=host, workload=workload, running=running, status=status
        )

    def teardown_workload(
        self, *, host: str, workload: str, user: str = "root", port: int = 22
    ) -> WorkloadTeardownResult:
        name = shlex.quote(workload)
        proc = self._run_remote(
            host=host, user=user, port=port, command=f"docker rm -f {name}"
        )
        return WorkloadTeardownResult(
            host=host, workload=workload, removed=proc.returncode == 0
        )
