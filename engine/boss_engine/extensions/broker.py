"""The extension broker: one RPC = one isolated child process (boss.extension/v1).

THE SAME ISOLATION AS boss.plugin/v1 (platform/runner.py, SubprocessPlugin.run), reused as a shape
rather than edited: `python -m boss_engine.extensions.child` with an EMPTY environment (PATH, a
PYTHONPATH to the engine, HOME at a throwaway directory, no bytecode), JSON lines over stdin and
stdout, stderr discarded, a hard timeout, and the child killed whatever happens. The child holds
no database connection and no credential. While it runs it may ASK the parent for:

    state.get / state.list     needs state.read     (namespace: this extension, this project)
    state.put / state.delete   needs state.write
    log                        any extension (kept on the outcome, capped)

The parent answers each ask in its own short transaction, after checking the manifest. A refused
ask fails the call ("permission denied: …"), exactly as v1 fails a hook.

Before spawning, the TRUST GATE (registry.InstalledExtension.verdict) must pass and the method's
capability must be declared (contract.RPC_METHODS); the child re-checks the tree checksum before it
imports anything. Every RPC carries `rpc: RPC_VERSION`.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .contract import CHILD_OPS, RPC_METHODS, RPC_VERSION
from .registry import ExtensionRefused, InstalledExtension
from . import state as state_store

MAX_LOG_LINES = 50
MAX_RESULT_BYTES = 1024 * 1024


@dataclass
class CallOutcome:
    ok: bool
    result: Any = None
    code: Optional[str] = None
    error: Optional[str] = None
    duration_ms: int = 0
    logs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "result": self.result, "code": self.code, "error": self.error,
                "durationMs": self.duration_ms, "logs": list(self.logs)}


def _isolated_env(workdir: str) -> dict[str, str]:
    """platform/runner.py's child environment, verbatim in shape: nothing of BOSS's own."""
    engine_root = str(Path(__file__).resolve().parents[2])
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": engine_root,
            "HOME": workdir, "PYTHONDONTWRITEBYTECODE": "1"}


def call(engine: Any, ext: InstalledExtension, method: str, params: dict[str, Any], *,
         project_id: Optional[str] = None, timeout: Optional[float] = None,
         extra: Optional[dict[str, Any]] = None) -> CallOutcome:
    """Run one RPC against the extension. Never raises for what the extension did: a refusal, a
    timeout or a crash is an outcome with ok=False and a code."""
    started = time.monotonic()
    try:
        manifest = ext.manifest                       # the trust gate
    except ExtensionRefused as exc:
        return CallOutcome(False, code=exc.code, error=str(exc))
    needed = RPC_METHODS.get(method)
    if needed is None:
        return CallOutcome(False, code="unknown_method", error=f"no RPC method {method}")
    if not manifest.has(needed):
        return CallOutcome(False, code="capability_not_declared",
                           error=f"{ext.extension_id} did not declare {needed}")
    limit = float(timeout if timeout is not None else manifest.spec.runtime.timeoutSeconds)
    caps = set(manifest.spec.capabilities)
    logs: list[str] = []
    request = {"rpc": RPC_VERSION, "method": method, "params": params,
               "context": {"extension_id": ext.extension_id, "version": ext.version,
                           "project_id": project_id, "extra": extra or {}}}
    workdir = tempfile.mkdtemp(prefix="boss-ext-")
    proc = subprocess.Popen(
        [sys.executable, "-m", "boss_engine.extensions.child", str(ext.directory),
         manifest.spec.entrypoint, ext.approved_checksum],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        env=_isolated_env(workdir), cwd=workdir)
    assert proc.stdin and proc.stdout
    lines: "queue.Queue[Optional[str]]" = queue.Queue()

    def pump() -> None:
        assert proc.stdout
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True, name=f"ext-broker-{ext.extension_id}").start()

    def send(obj: dict) -> None:
        assert proc.stdin
        proc.stdin.write(json.dumps(obj, default=str) + "\n")
        proc.stdin.flush()

    def done(outcome: CallOutcome) -> CallOutcome:
        outcome.duration_ms = int((time.monotonic() - started) * 1000)
        outcome.logs = logs
        return outcome

    deadline = started + limit
    try:
        send(request)
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return done(CallOutcome(False, code="timeout",
                                        error=f"{method} took longer than {limit:g}s"))
            try:
                line = lines.get(timeout=left)
            except queue.Empty:
                continue
            if line is None:
                return done(CallOutcome(False, code="extension_exited",
                                        error=f"{ext.extension_id} exited without a result"))
            if len(line) > MAX_RESULT_BYTES:
                return done(CallOutcome(False, code="result_too_large",
                                        error=f"a reply is at most {MAX_RESULT_BYTES} bytes"))
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            op = msg.get("op")
            if op == "result":
                return done(CallOutcome(True, result=msg.get("result")))
            if op in ("error", "refused"):
                return done(CallOutcome(False, code=str(msg.get("code") or op),
                                        error=str(msg.get("error") or "")[:2000]))
            if op not in CHILD_OPS:
                send({"error": f"unknown op {op}"})
                continue
            need = CHILD_OPS[op]
            if need is not None and need not in caps:
                send({"error": f"permission denied: {op} needs {need}, which "
                               f"{ext.extension_id} did not declare"})
                continue
            try:
                send(_answer(engine, ext, project_id, op, msg, logs))
            except state_store.StateError as exc:
                send({"error": str(exc)})
    except (BrokenPipeError, OSError) as exc:
        return done(CallOutcome(False, code="runner_error", error=type(exc).__name__))
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)


def _answer(engine: Any, ext: InstalledExtension, project_id: Optional[str], op: str, msg: dict,
            logs: list[str]) -> dict[str, Any]:
    if op == "log":
        if len(logs) < MAX_LOG_LINES:
            logs.append(str(msg.get("line", ""))[:2000])
        return {"ok": True}
    spec = ext.manifest.spec.state
    # THE NAMESPACE IS FILLED HERE, from the install the broker spawned — never from the child.
    with engine.begin() as connection:
        if op == "state.get":
            found, value = state_store.get(connection, ext.extension_id, project_id, msg.get("key"))
            return {"found": found, "value": value}
        if op == "state.list":
            return {"keys": state_store.list_keys(connection, ext.extension_id, project_id,
                                                  str(msg.get("prefix") or ""))}
        if op == "state.put":
            size = state_store.put(connection, ext.extension_id, project_id, msg.get("key"),
                                   msg.get("value"), max_value_bytes=spec.maxValueBytes,
                                   max_keys=spec.maxKeys)
            return {"ok": True, "bytes": size}
        if op == "state.delete":
            return {"ok": state_store.delete(connection, ext.extension_id, project_id, msg.get("key"))}
    return {"error": f"unknown op {op}"}
