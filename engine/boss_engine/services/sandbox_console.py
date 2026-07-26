"""Governed control-plane access to the execution plane (Sandbox Agent).

The control plane is the ONLY way the console reaches a sandbox: it proxies to
the Sandbox Agent and adds governance — every source read and every command is
audited, and a raw shell (break-glass) requires an explicit reason and emits an
elevated audit event (PRD §25). Residency-tier sandboxes are never reachable
this way (the agent for those runs in the customer boundary).
"""
from __future__ import annotations

from typing import Optional

import httpx
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event


def _base(settings: Settings) -> str:
    return (settings.sandbox_agent_url or "").rstrip("/")


def _auth_headers(settings: Settings) -> dict:
    return {"X-Sandbox-Token": settings.sandbox_token} if settings.sandbox_token else {}


def list_sandboxes(settings: Settings) -> dict:
    """Report the connected micro-VM execution host. Sean's Firecracker service exposes
    /v1/sandbox/health (VMs are single-tenant, ephemeral & isolated by design — no browse/
    exec API), so we surface the host status as proof the control plane is wired to it."""
    base = _base(settings)
    try:
        with httpx.Client(timeout=10) as c:
            r = c.get(f"{base}/v1/sandbox/health", headers=_auth_headers(settings))
        r.raise_for_status()
        h = r.json()
        return {"sandboxes": [], "host": {
            "connected": bool(h.get("ok")), "kind": "Firecracker micro-VM",
            "capacity": h.get("capacity"), "available": h.get("available"),
            "running": h.get("running"), "url": base}}
    except Exception as e:  # noqa: BLE001
        return {"sandboxes": [], "host": {"connected": False}, "unavailable": str(e)[:200]}


def list_helpers(settings: Settings) -> dict:
    """The governed helper allowlist (no break-glass needed), grouped for display."""
    with httpx.Client(timeout=10) as c:
        r = c.get(f"{_base(settings)}/v1/sandbox/helpers")
    r.raise_for_status()
    return r.json()


# ---- Micro-VM inspection (retained rootfs of a failed/incomplete task) ----------- #
# Sean's service holds a task's VM rootfs ~2h after it ends. These proxy his
# per-task endpoints so the console can root-cause a failure: browse /work/repo,
# view logs, and hold the VM longer. `sandbox_id` here is the sandbox-service task id.
_ROOT = "/work/repo"
_SKIP = {".git", "node_modules", "target", "build", "dist", "__pycache__", ".gradle", ".venv"}


def _fs(settings: Settings, sandbox_id: str, path: str) -> dict:
    with httpx.Client(timeout=20) as c:
        r = c.get(f"{_base(settings)}/v1/sandbox/tasks/{sandbox_id}/fs",
                  params={"path": path}, headers=_auth_headers(settings))
    r.raise_for_status()
    return r.json()


def get_tree(settings: Settings, sandbox_id: str, *, limit: int = 400) -> dict:
    """Breadth-first walk of the retained /work/repo, returned as a flat file list
    (the existing browse UI expects that shape)."""
    out: list[str] = []
    stack = [_ROOT]
    while stack and len(out) < limit:
        d = stack.pop(0)
        try:
            data = _fs(settings, sandbox_id, d)
        except Exception:  # noqa: BLE001
            continue
        for e in data.get("entries", []):
            name = e.get("name")
            if not name or name in _SKIP:
                continue
            full = d.rstrip("/") + "/" + name
            if e.get("dir"):
                stack.append(full)
            else:
                out.append(full[len(_ROOT) + 1:] if full.startswith(_ROOT + "/") else full)
            if len(out) >= limit:
                break
    return {"tree": sorted(out)}


def read_file(connection: Connection, settings: Settings, sandbox_id: str, path: str,
              actor: str = "operator") -> dict:
    abspath = path if path.startswith("/") else f"{_ROOT}/{path}"
    data = _fs(settings, sandbox_id, abspath)
    emit_event(connection, "sandbox", "sandbox.file_read", actor, "sandbox", sandbox_id, {"path": path})
    return {"content": data.get("content", ""), "truncated": data.get("truncated", False)}


def get_logs(settings: Settings, sandbox_id: str) -> dict:
    """Guest serial console + the code-agent's log from the retained VM."""
    with httpx.Client(timeout=20) as c:
        r = c.get(f"{_base(settings)}/v1/sandbox/tasks/{sandbox_id}/logs",
                  headers=_auth_headers(settings))
    r.raise_for_status()
    return r.json()


def hold(connection: Connection, settings: Settings, sandbox_id: str, *,
         hours: int = 2, actor: str = "operator") -> dict:
    """Extend the retention window on a VM so an investigation can continue — audited."""
    with httpx.Client(timeout=20) as c:
        r = c.post(f"{_base(settings)}/v1/sandbox/tasks/{sandbox_id}/hold",
                   json={"hours": hours}, headers=_auth_headers(settings))
    r.raise_for_status()
    emit_event(connection, "sandbox", "sandbox.hold", actor, "sandbox", sandbox_id, {"hours": hours})
    return r.json()


def exec_command(connection: Connection, settings: Settings, sandbox_id: str, command: str,
                 *, break_glass: bool = False, reason: Optional[str] = None,
                 actor: str = "operator") -> dict:
    # A retained rootfs is a read-only snapshot — there's no live process to exec into.
    return {"blocked": True,
            "detail": "Live exec isn't available on a retained micro-VM rootfs. Browse the "
                      "files and view the logs to root-cause; use Hold to keep the VM longer."}


def get_stats(settings: Settings, sandbox_id: str) -> dict:
    return {"container_status": "retained rootfs (read-only)", "cpu_pct": None,
            "mem_usage": None, "pids": None}


def destroy(settings: Settings, sandbox_id: str) -> dict:
    """Release a retained VM early (free the slot before the auto-reap)."""
    with httpx.Client(timeout=20) as c:
        r = c.delete(f"{_base(settings)}/v1/sandbox/tasks/{sandbox_id}",
                     headers=_auth_headers(settings))
    r.raise_for_status()
    return r.json() if r.content else {"destroyed": True}
