"""Where a plugin's hooks execute.

Built-in python plugins run in-process (P0 speed, decision 3 of the MVP proposal). Every other
python plugin (verified, community) runs OUT of process: a child `python -m
boss_engine.platform.runner` with an empty environment, talking JSON lines over stdin/stdout.
The child never holds a host connection or the vault: each `ctx.host.run`, `ctx.secrets.*` and
`ctx.log` is a request to the parent, which checks it against the manifest's declared
permissions before doing it:

    host.run        needs permissions.docker or permissions.subprocess
    secrets.put     needs permissions.secrets: write-scoped
    secrets.resolve needs read-scoped or write-scoped

Container-runtime plugins (`runtime.type: container`) are validated and listed; executing them
in an isolated runner is P1 (remote isolated runners), so `plugin_for` refuses them clearly.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from .contract import (
    CommandOutcome, Hook, HookContext, InfraPlugin, OperationResult, PluginSource, ResourcePlan,
    ResourceRequest, SecretRef,
)
from .registry import RegisteredPlugin, RegistryError, _load_entrypoint, load_plugin


def plugin_for(reg: RegisteredPlugin) -> InfraPlugin:
    if reg.manifest is None:
        raise RegistryError("plugin_invalid", f"{reg.id} has no valid manifest")
    if reg.manifest.spec.runtime.type == "container":
        raise RegistryError("runtime_unavailable",
                            f"{reg.id} is a container-runtime plugin; isolated container runners are P1")
    if reg.source == PluginSource.BUILTIN:
        return load_plugin(reg)
    return SubprocessPlugin(reg)


class PermissionDenied(Exception):
    pass


class SubprocessPlugin(InfraPlugin):
    def __init__(self, reg: RegisteredPlugin) -> None:
        self.reg = reg
        self.manifest = reg.manifest

    def run(self, ctx: HookContext) -> OperationResult:
        perms = self.manifest.spec.permissions
        engine_root = str(Path(__file__).resolve().parents[2])
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": engine_root,
               "HOME": str(ctx.workdir), "PYTHONDONTWRITEBYTECODE": "1"}
        timeout = self.manifest.spec.runtime.timeoutSeconds
        deadline = time.monotonic() + timeout
        proc = subprocess.Popen(
            [sys.executable, "-m", "boss_engine.platform.runner", str(self.reg.directory),
             self.manifest.spec.runtime.entrypoint or "", self.reg.checksum],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            env=env, cwd=str(ctx.workdir))
        assert proc.stdin and proc.stdout

        def send(obj: dict) -> None:
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()

        try:
            send({"ctx": _ctx_payload(ctx)})
            for line in proc.stdout:
                if time.monotonic() > deadline:
                    raise TimeoutError
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                op = msg.get("op")
                try:
                    if op == "result":
                        return OperationResult.model_validate(msg["result"])
                    if op == "log":
                        ctx.log(str(msg.get("line", ""))[:2000])
                        send({"ok": True})
                    elif op == "cancelled":
                        send({"value": bool(ctx.is_cancelled())})
                    elif op == "host":
                        if not (perms.docker or perms.subprocess):
                            raise PermissionDenied("host commands need permissions.docker or subprocess")
                        out = ctx.host.run(str(msg["command"]), timeout=int(msg.get("timeout") or 120),
                                           stdin_text=msg.get("stdin"))
                        send({"returncode": out.returncode, "stdout": out.stdout, "stderr": out.stderr})
                    elif op == "put":
                        if "write-scoped" not in perms.secrets:
                            raise PermissionDenied("storing a secret needs permissions.secrets: write-scoped")
                        send({"ref": ctx.secrets.put(str(msg["name"]), str(msg["value"])).secretRef})
                    elif op == "resolve":
                        if not perms.secrets:
                            raise PermissionDenied("reading a secret needs permissions.secrets")
                        send({"value": ctx.secrets.resolve(str(msg["ref"]))})
                    elif op == "delete":
                        if "write-scoped" not in perms.secrets:
                            raise PermissionDenied("deleting a secret needs permissions.secrets: write-scoped")
                        ctx.secrets.delete(str(msg["ref"]))
                        send({"ok": True})
                    else:
                        send({"error": "unknown op"})
                except PermissionDenied as exc:
                    send({"error": f"permission denied: {exc}"})
                    return ctx.fail("permission_denied", str(exc))
            return ctx.fail("plugin_exited", f"{self.reg.id} exited without a result")
        except TimeoutError:
            return ctx.fail("timeout", f"{ctx.hook.value} exceeded {timeout}s", retryable=True)
        except Exception as exc:  # broken pipe, a bad message: a failed operation
            return ctx.fail("runner_error", type(exc).__name__)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)


def _ctx_payload(ctx: HookContext) -> dict[str, Any]:
    return {"hook": ctx.hook.value, "operation_id": ctx.operation_id, "idempotency_key": ctx.idempotency_key,
            "resource": ctx.resource.model_dump(mode="json"), "resource_id": ctx.resource_id,
            "app_key": ctx.app_key, "project_id": ctx.project_id, "workdir": str(ctx.workdir),
            "plan": ctx.plan.model_dump(mode="json") if ctx.plan else None, "plan_digest": ctx.plan_digest,
            "state": ctx.state, "labels": ctx.labels, "plugin_id": ctx.plugin_id,
            "plugin_version": ctx.plugin_version, "host_address": getattr(ctx.host, "address", "127.0.0.1")}


# --------------------------------------------------------------------------------------------
# The child side
# --------------------------------------------------------------------------------------------

class _Channel:
    def __init__(self) -> None:
        self.inp, self.out = sys.stdin, sys.stdout

    def ask(self, obj: dict) -> dict:
        self.out.write(json.dumps(obj) + "\n")
        self.out.flush()
        reply = json.loads(self.inp.readline() or "{}")
        if "error" in reply:
            raise PermissionError(reply["error"])
        return reply


class _ProxyBroker:
    def __init__(self, ch: _Channel) -> None:
        self.ch = ch

    def put(self, name: str, value: str) -> SecretRef:
        return SecretRef(secretRef=self.ch.ask({"op": "put", "name": name, "value": value})["ref"])

    def resolve(self, ref: Any) -> str:
        raw = ref.secretRef if isinstance(ref, SecretRef) else str(ref)
        return self.ch.ask({"op": "resolve", "ref": raw})["value"]

    def delete(self, ref: Any) -> None:
        raw = ref.secretRef if isinstance(ref, SecretRef) else str(ref)
        self.ch.ask({"op": "delete", "ref": raw})


class _ProxyHost:
    def __init__(self, ch: _Channel, address: str) -> None:
        self.ch, self.address = ch, address

    def run(self, command: str, *, timeout: int = 120, stdin_text: Optional[str] = None) -> CommandOutcome:
        r = self.ch.ask({"op": "host", "command": command, "timeout": timeout, "stdin": stdin_text})
        return CommandOutcome(int(r["returncode"]), r.get("stdout", ""), r.get("stderr", ""))


def _child(directory: str, entrypoint: str, checksum: str) -> int:
    from .contract import load_manifest
    ch = _Channel()
    payload = json.loads(sys.stdin.readline())["ctx"]
    manifest = load_manifest(directory)
    plugin = _load_entrypoint(directory, entrypoint, checksum, manifest)
    ctx = HookContext(
        hook=Hook(payload["hook"]), operation_id=payload["operation_id"],
        idempotency_key=payload["idempotency_key"], resource=ResourceRequest.model_validate(payload["resource"]),
        resource_id=payload["resource_id"], app_key=payload["app_key"], project_id=payload.get("project_id"),
        secrets=_ProxyBroker(ch), workdir=Path(payload["workdir"]),
        plan=ResourcePlan.model_validate(payload["plan"]) if payload.get("plan") else None,
        plan_digest=payload.get("plan_digest"), state=payload.get("state") or {},
        labels=payload.get("labels") or {},
        is_cancelled=lambda: bool(ch.ask({"op": "cancelled"}).get("value")),
        log=lambda line: ch.ask({"op": "log", "line": line}),
        plugin_id=payload.get("plugin_id", ""), plugin_version=payload.get("plugin_version", ""),
        host=_ProxyHost(ch, payload.get("host_address") or "127.0.0.1"))
    result = plugin.run(ctx)
    sys.stdout.write(json.dumps({"op": "result", "result": result.model_dump(mode="json")}) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(_child(*sys.argv[1:4]))
