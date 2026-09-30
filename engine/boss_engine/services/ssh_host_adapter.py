from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import time
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


@dataclass
class JobStartResult:
    host: str
    job: str
    container_id: str
    started: bool


@dataclass
class JobStatusResult:
    host: str
    job: str
    state: str  # running | exited | dead | not_found | unknown
    exit_code: int | None
    finished: bool


@dataclass
class JobRunResult:
    host: str
    job: str
    state: str
    exit_code: int | None
    finished: bool
    logs: str


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

    # -- one-shot jobs (ingestion / backfill) ------------------------------
    def run_job(
        self,
        *,
        host: str,
        job: str,
        image: str,
        user: str = "root",
        port: int = 22,
        env: dict[str, str] | None = None,
        ports: list[str] | None = None,
        volumes: list[str] | None = None,
        command: str | None = None,
    ) -> JobStartResult:
        """Start a one-shot container job (no auto-restart) that runs to completion.
        The exited container is retained so status + logs can be read; call
        ``remove_job`` (or ``run_job_to_completion(remove=True)``) to clean up."""
        env = env or {}
        ports = ports or []
        volumes = volumes or []
        name = shlex.quote(job)
        parts = ["docker", "rm", "-f", name, ">/dev/null", "2>&1", ";",
                 "docker", "run", "-d", "--name", name]
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
                f"ssh_host job start failed on {host}: {(proc.stderr or proc.stdout or '').strip()[:500]}"
            )
        cid = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""
        return JobStartResult(host=host, job=job, container_id=cid, started=True)

    def job_status(
        self, *, host: str, job: str, user: str = "root", port: int = 22
    ) -> JobStatusResult:
        name = shlex.quote(job)
        proc = self._run_remote(
            host=host,
            user=user,
            port=port,
            command=f"docker inspect -f '{{{{.State.Status}}}} {{{{.State.ExitCode}}}}' {name}",
        )
        if proc.returncode != 0:
            return JobStatusResult(
                host=host, job=job, state="not_found", exit_code=None, finished=False
            )
        fields = (proc.stdout or "").strip().split()
        state = fields[0] if fields else "unknown"
        finished = state in {"exited", "dead"}
        exit_code: int | None = None
        if finished and len(fields) > 1:
            try:
                exit_code = int(fields[1])
            except ValueError:
                exit_code = None
        return JobStatusResult(
            host=host, job=job, state=state, exit_code=exit_code, finished=finished
        )

    def job_logs(
        self, *, host: str, job: str, user: str = "root", port: int = 22, tail: int = 200
    ) -> str:
        name = shlex.quote(job)
        proc = self._run_remote(
            host=host,
            user=user,
            port=port,
            command=f"docker logs --tail {int(tail)} {name} 2>&1",
        )
        return (proc.stdout or proc.stderr or "").strip()

    def remove_job(
        self, *, host: str, job: str, user: str = "root", port: int = 22
    ) -> bool:
        name = shlex.quote(job)
        proc = self._run_remote(
            host=host, user=user, port=port, command=f"docker rm -f {name}"
        )
        return proc.returncode == 0

    def run_job_to_completion(
        self,
        *,
        host: str,
        job: str,
        image: str,
        user: str = "root",
        port: int = 22,
        env: dict[str, str] | None = None,
        volumes: list[str] | None = None,
        command: str | None = None,
        timeout: int = 900,
        poll_interval: int = 5,
        remove: bool = True,
        log_tail: int = 200,
    ) -> JobRunResult:
        """Start a job, poll until it exits (or ``timeout`` seconds elapse), collect
        logs + exit code, then optionally remove the container. This is the
        ``BOSS initiates ingestion, embeddings run in customer infra`` primitive."""
        self.run_job(
            host=host,
            job=job,
            image=image,
            user=user,
            port=port,
            env=env,
            volumes=volumes,
            command=command,
        )
        status = self.job_status(host=host, job=job, user=user, port=port)
        elapsed = 0
        while not status.finished and elapsed < timeout:
            time.sleep(poll_interval)
            elapsed += poll_interval
            status = self.job_status(host=host, job=job, user=user, port=port)
        logs = self.job_logs(host=host, job=job, user=user, port=port, tail=log_tail)
        result = JobRunResult(
            host=host,
            job=job,
            state=status.state,
            exit_code=status.exit_code,
            finished=status.finished,
            logs=logs,
        )
        if remove:
            self.remove_job(host=host, job=job, user=user, port=port)
        return result
