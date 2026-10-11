"""The contract harness the official plugins are certified with.

Runs a plugin's hooks against REAL containers and checks the boss.plugin/v1 rules:
  * every hook returns a valid OperationResult;
  * validate / plan / verify / observe change nothing; verify proves a real `connection`;
  * provision is idempotent: twice gives the same resourceId, the same binding refs and the
    same container (not recreated);
  * no secret value (anything put in the broker) appears in a result, a log line, an artifact
    name or a file the plugin wrote outside `backups/`;
  * after destroy: no container, volume, network or vault secret is left.

Every object a test creates carries the label `boss.test-run=<RUN_ID>`; `teardown_run` removes
anything with it even when a test fails half way.
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

from boss_engine.platform.contract import (
    Hook, HookContext, OperationResult, OperationStatus, ResourcePlan, ResourceRequest, SecretRef,
    idempotency_key,
)

RUN_ID = "bit" + secrets.token_hex(4)
EVIDENCE_DIR = os.environ.get("BOSS_INFRA_EVIDENCE_DIR")


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


class MemoryBroker:
    """The vault, for tests: values by name, refs out."""

    def __init__(self, env: str, app: str) -> None:
        self.prefix = f"app_secret://{env}/{app}/"
        self.store: dict[str, str] = {}
        self.ever: set[str] = set()

    def put(self, name: str, value: str) -> SecretRef:
        self.store[name] = value
        self.ever.add(value)
        return SecretRef(secretRef=self.prefix + name)

    def resolve(self, ref) -> str:
        ref = ref.secretRef if isinstance(ref, SecretRef) else str(ref)
        return self.store[ref.removeprefix(self.prefix)]

    def delete(self, ref) -> None:
        ref = ref.secretRef if isinstance(ref, SecretRef) else str(ref)
        self.store.pop(ref.removeprefix(self.prefix), None)


class Harness:
    def __init__(self, plugin, workdir: Path, *, app_key: Optional[str] = None, broker: Optional[MemoryBroker] = None,
                 env: str = "deploy") -> None:
        self.plugin = plugin
        self.workdir = Path(workdir)
        self.app_key = app_key or f"{RUN_ID}-{secrets.token_hex(2)}"
        self.broker = broker or MemoryBroker(env, self.app_key)
        self.results: list[OperationResult] = []
        self.logs: list[str] = []
        self.timings: dict[str, list[int]] = {}
        self.extra_secrets: set[str] = set()

    # -- calling hooks ------------------------------------------------------------------------
    def ctx(self, hook: Hook, request: ResourceRequest, *, state: Optional[dict] = None,
            plan: Optional[ResourcePlan] = None) -> HookContext:
        rid = request.resource_id(self.app_key)
        m = self.plugin.manifest
        return HookContext(hook=hook, operation_id=f"op-{secrets.token_hex(4)}",
                           idempotency_key=idempotency_key(m.id, m.version, rid, hook, None),
                           resource=request, resource_id=rid, app_key=self.app_key, secrets=self.broker,
                           workdir=self.workdir, state=dict(state or {}), plan=plan,
                           labels={"boss.test-run": RUN_ID}, log=self.logs.append,
                           deadline=time.monotonic() + 600)

    def call(self, hook: str, request: ResourceRequest, *, state: Optional[dict] = None,
             plan: Optional[ResourcePlan] = None) -> OperationResult:
        h = Hook(hook)
        out = self.plugin.run(self.ctx(h, request, state=state, plan=plan))
        assert isinstance(out, OperationResult)
        OperationResult.model_validate_json(out.model_dump_json())  # round-trips as the contract
        self.results.append(out)
        self.timings.setdefault(h.value, []).append(out.durationMs or 0)
        return out

    # -- the standard lifecycle ---------------------------------------------------------------
    def lifecycle(self, request: ResourceRequest, *, state: Optional[dict] = None, between=None,
                  container_service: Optional[str] = None, clean: bool = True) -> dict[str, Any]:
        """validate, plan, provision x2 (idempotent), configure, verify, observe, [between], destroy.
        Returns the evidence summary; asserts every contract rule on the way."""
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
        state = {**base, **second.state}

        if self.plugin.manifest.implements("configure"):
            c = self.call("configure", request, state=state)
            assert c.ok, c.errors
            state = {**state, **c.state}
        ver = self.call("verify", request, state=state)
        assert ver.ok and ver.connected, (ver.errors, ver.evidence)
        if self.plugin.manifest.implements("observe"):
            o = self.call("observe", request, state=state)
            assert o.ok, o.errors
        if between:
            state = between(state) or state

        refused = self.call("destroy", request, state=state)  # no approved plan: refused
        assert refused.status is OperationStatus.REFUSED
        dplan = self.call("plan", request, state={**state, "desired": "absent"}).plan
        d = self.call("destroy", request, state=state, plan=dplan)
        assert d.ok, d.errors
        if clean:
            self.assert_clean()
        self.assert_redacted()
        return {"verify": [e.model_dump() for e in ver.evidence], "timingsMs": self.timings}

    # -- checks -------------------------------------------------------------------------------
    def container_id(self, service: str) -> str:
        proj = f"bossapp-{self.app_key}-deploy"
        return sh("docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={proj}",
                  "--filter", f"label=com.docker.compose.service={service}")

    def assert_clean(self) -> None:
        proj = f"bossapp-{self.app_key}-deploy"
        assert not sh("docker", "ps", "-aq", "--filter", f"label=boss.app={self.app_key}"), "containers left behind"
        assert not sh("docker", "volume", "ls", "-q", "--filter", f"label=boss.app={self.app_key}"), "volumes left behind"
        assert not sh("docker", "network", "ls", "-q", "--filter", f"name=^{proj}_"), "network left behind"
        assert not self.broker.store, f"vault secrets left behind: {sorted(self.broker.store)}"

    def assert_redacted(self) -> None:
        values = {v for v in self.broker.ever | self.extra_secrets if len(v) >= 6}
        blobs = [r.model_dump_json() for r in self.results] + self.logs
        for f in self.workdir.rglob("*"):
            if f.is_file() and "backups" not in f.parts:
                blobs.append(f.read_text(errors="replace"))
            blobs.append(str(f))
        for v in values:
            for b in blobs:
                assert v not in b, "a secret value leaked into a result, log, file or artifact name"

    def record(self, name: str, summary: dict) -> None:
        if EVIDENCE_DIR:
            Path(EVIDENCE_DIR).mkdir(parents=True, exist_ok=True)
            (Path(EVIDENCE_DIR) / f"{name}.json").write_text(json.dumps(summary, indent=2, default=str))


def teardown_run() -> None:
    """Remove everything this test run labelled, and the compose networks of its projects."""
    for cid in sh("docker", "ps", "-aq", "--filter", f"label=boss.test-run={RUN_ID}").split():
        subprocess.run(["docker", "rm", "-f", "-v", cid], capture_output=True)
    for vol in sh("docker", "volume", "ls", "-q", "--filter", f"label=boss.test-run={RUN_ID}").split():
        subprocess.run(["docker", "volume", "rm", "-f", vol], capture_output=True)
    for net in sh("docker", "network", "ls", "--format", "{{.Name}}", "--filter", f"name=bossapp-{RUN_ID}").split():
        subprocess.run(["docker", "network", "rm", net], capture_output=True)


WHOAMI = "traefik/whoami:v1.11@sha256:200689790a0a0ea48ca45992e0450bc26ccab5307375b41c84dfc4f2475937ab"


def app_compose(h: Harness, services: dict) -> Path:
    """An app's own compose file (not a resource fragment), labelled for cleanup."""
    lab = {"boss.app": h.app_key, "boss.test-run": RUN_ID}
    body = {"services": {name: {**svc, "labels": lab} for name, svc in services.items()}}
    import yaml
    p = h.workdir / "app" / "docker-compose.yml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(body))
    return p


def compose_up_app(h: Harness, path: Path, *services: str) -> None:
    proj = f"bossapp-{h.app_key}-deploy"
    subprocess.run(["docker", "compose", "-p", proj, "-f", str(path), "up", "-d", "--wait", *services],
                   check=True, capture_output=True, timeout=180)
