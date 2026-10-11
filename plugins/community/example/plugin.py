"""community.example-cache: a Valkey cache in one container. The reference community plugin.

Read this file next to docs/community/plugin-authoring.md. It shows every rule the contract
harness checks:
  * commands run through ctx.host.run (the deploy's host, or this machine in the harness);
  * everything created carries the resource's labels (sdk.docker_labels_args);
  * the password goes to Valkey through stdin -> an env var, never on a command line or in a log,
    and leaves the plugin only inside CACHE_URL, through ctx.secrets.put;
  * provision is idempotent: an existing container is reused, its password read back from it;
  * the port binds to 127.0.0.1 unless the request is public (which BOSS approves first);
  * verify opens a real connection (AUTH + PING over RESP), not "the command returned 0".
"""
from __future__ import annotations

import hashlib
import json
import shlex
import socket
import time
from urllib.parse import urlparse

from boss_engine.platform.contract import (
    CostEstimate, Evidence, HookContext, InfraPlugin, OperationResult, PlannedChange, PortSpec,
    ResourcePlan,
)
from boss_engine.platform.sdk import bind_address, docker_labels_args, free_port, generate_password, slug

IMAGE = "valkey/valkey:8.1.3-alpine@sha256:d827e7f7552cdee40cc7482dbae9da020f42bc47669af6f71182a4ef76a22773"
PORT = 6379
CONFIG_KEYS = {"maxmemory"}


def _names(ctx: HookContext) -> tuple[str, str]:
    scope = hashlib.sha256(json.dumps([ctx.project_id, ctx.labels], sort_keys=True).encode()).hexdigest()[:6]
    name = f"boss-{slug(ctx.resource_id, 40)}-{scope}"
    return name, f"{name}-data"


def _resp(*parts: str) -> bytes:
    out = f"*{len(parts)}\r\n".encode()
    for p in parts:
        b = p.encode()
        out += b"$%d\r\n%s\r\n" % (len(b), b)
    return out


class ExampleCache(InfraPlugin):

    # -- read-only -------------------------------------------------------------------------

    def validate(self, ctx: HookContext) -> OperationResult:
        problems = []
        if ctx.resource.engine not in (None, "valkey"):
            problems.append(f"engine {ctx.resource.engine} is not valkey")
        unknown = sorted(set(ctx.resource.config) - CONFIG_KEYS)
        if unknown:
            problems.append(f"unknown config: {', '.join(unknown)}")
        if problems:
            return ctx.result("invalid", errors=[{"code": "invalid_request", "message": "; ".join(problems)}])
        return ctx.result()

    def plan(self, ctx: HookContext) -> OperationResult:
        name, volume = _names(ctx)
        exists = self._container(ctx, name) is not None
        binding = ctx.resource.bindings.get("CACHE_URL", "CACHE_URL")
        plan = ResourcePlan(
            resourceId=ctx.resource_id, pluginId=ctx.plugin_id, pluginVersion=ctx.plugin_version,
            mode=ctx.resource.mode,
            changes=[PlannedChange(action="noop" if exists else "create", target=f"container {name}"),
                     PlannedChange(action="noop" if exists else "create", target=f"volume {volume}")],
            ports=[PortSpec(name="valkey", containerPort=PORT, public=ctx.resource.public)],
            publicAccess=ctx.resource.public, bindings=[binding],
            cost=CostEstimate(monthlyUsd=0, basis="a local container"),
            limitations=["single node, no replication", "no backup/restore"])
        return ctx.result(plan=plan)

    # -- mutating --------------------------------------------------------------------------

    def provision(self, ctx: HookContext) -> OperationResult:
        name, volume = _names(ctx)
        found = self._container(ctx, name)
        if found is None:
            port = free_port()
            password = generate_password()
            labels = docker_labels_args(ctx)
            ctx.host.run(f"docker volume create {labels} {shlex.quote(volume)}", timeout=60)
            # The password arrives on stdin and is handed to docker as an env var by name only.
            cmd = (f"BOSS_PW=$(cat) docker run -d --name {shlex.quote(name)} {labels} -e BOSS_PW "
                   f"-p {bind_address(ctx)}:{port}:{PORT} -v {shlex.quote(volume)}:/data "
                   f"--entrypoint sh {IMAGE} -c 'exec valkey-server --appendonly yes --requirepass \"$BOSS_PW\"'")
            out = ctx.host.run(cmd, timeout=300, stdin_text=password)
            if out.returncode != 0:
                ctx.log(f"docker run failed: exit {out.returncode}")
                return ctx.fail("start_failed", "the valkey container did not start", retryable=True)
        else:
            port, password = found
        if not self._ready(ctx, name):
            return ctx.fail("not_ready", "valkey did not answer PING within 60s", retryable=True)
        url = f"redis://:{password}@{ctx.host.address}:{port}/0"
        ref = ctx.secrets.put("CACHE_URL", url)
        env_name = ctx.resource.bindings.get("CACHE_URL", "CACHE_URL")
        return ctx.result(bindings={env_name: ref}, artifacts=[f"container:{name}", f"volume:{volume}"],
                          state={"container": name, "volume": volume, "hostPort": port,
                                 "bindingRef": ref.secretRef},
                          evidence=[Evidence(check="container running", passed=True, kind="healthcheck")])

    def configure(self, ctx: HookContext) -> OperationResult:
        name = ctx.state.get("container") or _names(ctx)[0]
        out = ctx.host.run(f"docker port {shlex.quote(name)} {PORT}", timeout=30)
        bound = [line.strip() for line in out.stdout.splitlines() if line.strip()]
        private = all(b.startswith("127.0.0.1:") for b in bound) and bool(bound)
        ok = private or ctx.resource.public
        return ctx.result(None if ok else "failed",
                          evidence=[Evidence(check="port bound to 127.0.0.1", passed=private or ctx.resource.public,
                                             kind="config", detail=", ".join(bound))])

    def verify(self, ctx: HookContext) -> OperationResult:
        ref = ctx.state.get("bindingRef")
        if not ref:
            return ctx.fail("not_provisioned", "no CACHE_URL binding to verify")
        u = urlparse(ctx.secrets.resolve(ref))
        try:
            with socket.create_connection((u.hostname, u.port), timeout=5) as s:
                s.sendall(_resp("AUTH", u.password or "") + _resp("PING"))
                reply = b""
                deadline = time.monotonic() + 5
                while reply.count(b"\r\n") < 2 and time.monotonic() < deadline:
                    chunk = s.recv(256)
                    if not chunk:
                        break
                    reply += chunk
        except OSError as exc:
            return ctx.fail("connect_failed", f"could not connect: {type(exc).__name__}", retryable=True,
                            evidence=[Evidence(check="AUTH + PING", passed=False, kind="connection")])
        passed = reply.startswith(b"+OK\r\n+PONG")
        return ctx.result(None if passed else "failed",
                          evidence=[Evidence(check="AUTH + PING", passed=passed, kind="connection",
                                             detail="PONG" if passed else "unexpected reply")])

    def observe(self, ctx: HookContext) -> OperationResult:
        name = ctx.state.get("container") or _names(ctx)[0]
        out = ctx.host.run(f"docker inspect --format '{{{{.State.Status}}}}' {shlex.quote(name)}", timeout=30)
        status = out.stdout.strip() or "missing"
        return ctx.result(observed={"health": "up" if status == "running" else "down", "container": status},
                          evidence=[Evidence(check="container status", passed=status == "running", kind="observe")])

    def destroy(self, ctx: HookContext) -> OperationResult:
        name, volume = _names(ctx)
        name, volume = ctx.state.get("container", name), ctx.state.get("volume", volume)
        ctx.host.run(f"docker rm -f -v {shlex.quote(name)} >/dev/null 2>&1; "
                     f"docker volume rm -f {shlex.quote(volume)} >/dev/null 2>&1; true", timeout=120)
        if ctx.state.get("bindingRef"):
            ctx.secrets.delete(ctx.state["bindingRef"])
        gone = self._container(ctx, name) is None
        return ctx.result(None if gone else "failed",
                          evidence=[Evidence(check="container removed", passed=gone, kind="cleanup")])

    # -- helpers ---------------------------------------------------------------------------

    def _container(self, ctx: HookContext, name: str):
        """(host port, password) of an existing container, else None. The password is read back
        from the container so a second provision returns the same binding."""
        out = ctx.host.run(f"docker inspect --format '{{{{json .}}}}' {shlex.quote(name)} 2>/dev/null", timeout=30)
        if out.returncode != 0 or not out.stdout.strip():
            return None
        info = json.loads(out.stdout)
        password = next((e.split("=", 1)[1] for e in info["Config"].get("Env") or [] if e.startswith("BOSS_PW=")), "")
        bindings = (info.get("HostConfig") or {}).get("PortBindings") or {}
        port = int(((bindings.get(f"{PORT}/tcp") or [{}])[0]).get("HostPort") or 0)
        return port, password

    def _ready(self, ctx: HookContext, name: str) -> bool:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            out = ctx.host.run(f"docker exec {shlex.quote(name)} sh -c "
                               "'valkey-cli --no-auth-warning -a \"$BOSS_PW\" ping'", timeout=15)
            if "PONG" in out.stdout:
                return True
            time.sleep(0.5)
        return False
