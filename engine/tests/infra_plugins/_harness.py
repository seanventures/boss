"""The contract harness the official plugins are certified with, on top of the platform's.

Every hook runs through `IsolatedHost`: a HostRunner (contract 1.1 `ctx.host`) that runs each
command in `env -i bash`, so a plugin that leaned on this process's environment, or passed a
value any way but the host channel, fails here as it would over SSH. The broker is the
platform's MemoryBroker (only declared binding names; refs scoped to one app).

Checked on the way (assertions in `lifecycle`):
  * every hook returns a valid OperationResult;
  * validate / plan change nothing; verify proves a real `connection`;
  * provision is idempotent: twice gives the same resourceId, the same binding refs and the same
    container (not recreated);
  * no value the broker saw appears in a result or a log line;
  * after destroy: no container, volume or network of the app, and no vault secret, is left.

Every object a test creates carries `boss.test-run=<RUN_ID>`; `teardown_run` removes anything with
it even when a test fails half way.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

import yaml

from boss_engine.platform.bindings import MemoryBroker, Redactor
from boss_engine.platform.contract import (
    CommandOutcome, Hook, HookContext, OperationResult, OperationStatus, ResourcePlan, ResourceRequest,
    idempotency_key,
)

RUN_ID = "bit" + secrets.token_hex(4)
EVIDENCE_DIR = os.environ.get("BOSS_INFRA_EVIDENCE_DIR")
WHOAMI = "traefik/whoami:v1.11@sha256:200689790a0a0ea48ca45992e0450bc26ccab5307375b41c84dfc4f2475937ab"


def docker_ok() -> bool:
    if os.environ.get("BOSS_RUN_DOCKER_LIFECYCLE_TEST") != "1":
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except Exception:
        return False


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sh(*argv: str) -> str:
    return subprocess.run(list(argv), capture_output=True, text=True).stdout.strip()


class IsolatedHost:
    """ctx.host with nothing inherited: `env -i` keeps only PATH and HOME (for the docker CLI)."""
    address = "127.0.0.1"

    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, command: str, *, timeout: int = 120, stdin_text: Optional[str] = None) -> CommandOutcome:
        self.commands.append(command)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp")}
        try:
            p = subprocess.run(["env", "-i", *(f"{k}={v}" for k, v in env.items()), "bash", "-c", command],
                               input=stdin_text, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return CommandOutcome(124, "", f"timed out after {timeout}s")
        return CommandOutcome(p.returncode, p.stdout, p.stderr)


class Harness:
    def __init__(self, plugin, workdir: Path, *, app_key: Optional[str] = None) -> None:
        self.plugin = plugin
        self.workdir = Path(workdir)
        self.app_key = app_key or f"{RUN_ID}-{secrets.token_hex(2)}"
        self.redactor = Redactor()
        self.broker = MemoryBroker(app_key=self.app_key, manifest=plugin.manifest, redactor=self.redactor)
        self.host = IsolatedHost()
        self.results: list[OperationResult] = []
        self.logs: list[str] = []
        self.timings: dict[str, list[int]] = {}

    def ctx(self, hook: Hook, request: ResourceRequest, *, state: Optional[dict] = None,
            plan: Optional[ResourcePlan] = None) -> HookContext:
        self.broker.request = request
        rid = request.resource_id(self.app_key)
        m = self.plugin.manifest
        return HookContext(hook=hook, operation_id=f"op-{secrets.token_hex(4)}",
                           idempotency_key=idempotency_key(m.id, m.version, rid, hook, None),
                           resource=request, resource_id=rid, app_key=self.app_key, secrets=self.broker,
                           workdir=self.workdir, state=dict(state or {}), plan=plan, plan_digest="sha256:test",
                           labels={"boss.test-run": RUN_ID}, log=self.logs.append,
                           deadline=time.monotonic() + 600, host=self.host)

    def call(self, hook: str, request: ResourceRequest, *, state: Optional[dict] = None,
             plan: Optional[ResourcePlan] = None) -> OperationResult:
        h = Hook(hook)
        out = getattr(self.plugin, h.value)(self.ctx(h, request, state=state, plan=plan))
        assert isinstance(out, OperationResult)
        OperationResult.model_validate_json(out.model_dump_json())  # round-trips as the contract
        self.results.append(out)
        self.timings.setdefault(h.value, []).append(out.durationMs or 0)
        return out

    def lifecycle(self, request: ResourceRequest, *, state: Optional[dict] = None, between=None,
                  container_service: Optional[str] = None, clean: bool = True) -> dict[str, Any]:
        """validate, plan, provision x2 (idempotent), configure, verify, observe, [between], destroy."""
        base = dict(state or {})
        v = self.call("validate", request, state=base)
        assert v.status is OperationStatus.VALID, v.errors
        p = self.call("plan", request, state=base)
        assert p.ok and p.plan is not None and p.plan.resourceId == request.resource_id(self.app_key)
        assert not p.plan.publicAccess or request.public, "plan publishes a port nobody asked for"

        first = self.call("provision", request, state=base)
        assert first.ok, first.errors
        cid1 = self.container_id(container_service) if container_service else None
        second = self.call("provision", request, state={**base, **first.state})
        assert second.ok, second.errors
        assert second.resourceId == first.resourceId
        assert second.model_dump()["bindings"] == first.model_dump()["bindings"], "provision is not idempotent"
        if container_service:
            assert cid1 and self.container_id(container_service) == cid1, "second provision recreated the container"
        st = {**base, **second.state}

        if self.plugin.manifest.implements("configure"):
            c = self.call("configure", request, state=st)
            assert c.ok, c.errors
            st = {**st, **c.state}
        ver = self.call("verify", request, state=st)
        assert ver.ok and ver.connected, (ver.errors, ver.evidence)
        if self.plugin.manifest.implements("observe"):
            o = self.call("observe", request, state=st)
            assert o.ok, o.errors
        if between:
            st = between(st) or st

        dplan = self.call("plan", request, state={**st, "desired": "absent"}).plan
        assert dplan.destructive
        d = self.call("destroy", request, state=st, plan=dplan)
        assert d.ok, d.errors
        if clean:
            self.assert_clean()
        self.assert_redacted()
        return {"verify": [e.model_dump() for e in ver.evidence], "timingsMs": self.timings,
                "provisionRecreated": False if container_service else None}

    def container_id(self, service: str) -> str:
        return sh("docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project=bossapp-{self.app_key}-deploy",
                  "--filter", f"label=com.docker.compose.service={service}")

    def assert_clean(self) -> None:
        proj = f"bossapp-{self.app_key}-deploy"
        assert not sh("docker", "ps", "-aq", "--filter", f"label=boss.app={self.app_key}"), "containers left behind"
        assert not sh("docker", "volume", "ls", "-q", "--filter", f"label=boss.app={self.app_key}"), "volumes left behind"
        assert not sh("docker", "network", "ls", "-q", "--filter", f"name=^{proj}_"), "network left behind"
        assert not self.broker.store, f"vault secrets left behind: {sorted(self.broker.store)}"

    def assert_redacted(self) -> None:
        for blob in [r.model_dump_json() for r in self.results] + self.logs:
            assert not self.redactor.leaks(blob), "a secret value leaked into a result or a log line"
        for command in self.host.commands:  # values travel on stdin, never in a command line
            assert not self.redactor.leaks(command), "a secret value is in a command line"

    def record(self, name: str, summary: dict) -> None:
        if EVIDENCE_DIR:
            Path(EVIDENCE_DIR).mkdir(parents=True, exist_ok=True)
            (Path(EVIDENCE_DIR) / f"{name}.json").write_text(json.dumps(summary, indent=2, default=str))


def app_compose(h: Harness, services: dict) -> Path:
    """An app's own compose file (not a resource fragment), labelled for cleanup."""
    lab = {"boss.app": h.app_key, "boss.test-run": RUN_ID}
    p = h.workdir / "app" / "docker-compose.yml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump({"services": {n: {**svc, "labels": lab} for n, svc in services.items()}}))
    return p


def compose_up(h: Harness, files: list[Path], *services: str, env: Optional[dict] = None) -> None:
    subprocess.run(["docker", "compose", "-p", f"bossapp-{h.app_key}-deploy", *sum((["-f", str(f)] for f in files), []),
                    "up", "-d", "--wait", *services], check=True, capture_output=True, timeout=240,
                   env={**os.environ, **(env or {})})


def teardown_run() -> None:
    """Remove everything this test run labelled, and the compose networks of its projects."""
    for cid in sh("docker", "ps", "-aq", "--filter", f"label=boss.test-run={RUN_ID}").split():
        subprocess.run(["docker", "rm", "-f", "-v", cid], capture_output=True)
    for vol in sh("docker", "volume", "ls", "-q", "--filter", f"label=boss.test-run={RUN_ID}").split():
        subprocess.run(["docker", "volume", "rm", "-f", vol], capture_output=True)
    for net in sh("docker", "network", "ls", "--format", "{{.Name}}", "--filter", f"name=bossapp-{RUN_ID}").split():
        subprocess.run(["docker", "network", "rm", net], capture_output=True)
