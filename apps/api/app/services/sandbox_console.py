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


def get_sandbox(settings: Settings, sandbox_id: str) -> dict:
    with httpx.Client(timeout=20) as c:
        r = c.get(f"{_base(settings)}/v1/sandboxes/{sandbox_id}")
    r.raise_for_status()
    return r.json()


def get_tree(settings: Settings, sandbox_id: str) -> dict:
    with httpx.Client(timeout=20) as c:
        r = c.get(f"{_base(settings)}/v1/sandboxes/{sandbox_id}/tree")
    r.raise_for_status()
    return r.json()


def read_file(connection: Connection, settings: Settings, sandbox_id: str, path: str,
              actor: str = "operator") -> dict:
    tenant = get_sandbox(settings, sandbox_id).get("tenant_id", "unknown")
    with httpx.Client(timeout=20) as c:
        r = c.get(f"{_base(settings)}/v1/sandboxes/{sandbox_id}/file", params={"path": path})
    r.raise_for_status()
    emit_event(connection, tenant, "sandbox.file_read", actor, "sandbox", sandbox_id, {"path": path})
    return r.json()


def exec_command(connection: Connection, settings: Settings, sandbox_id: str, command: str,
                 *, break_glass: bool = False, reason: Optional[str] = None,
                 actor: str = "operator") -> dict:
    if break_glass and not (reason or "").strip():
        raise ValueError("break-glass requires a reason")
    tenant = get_sandbox(settings, sandbox_id).get("tenant_id", "unknown")
    with httpx.Client(timeout=320) as c:
        r = c.post(f"{_base(settings)}/v1/sandboxes/{sandbox_id}/exec",
                   json={"command": command, "break_glass": break_glass, "reason": reason})
    if r.status_code == 403:
        # allowlist rejection — surface as a clear governance message
        return {"blocked": True, "detail": r.json().get("detail", "command not allowed")}
    r.raise_for_status()
    # audit: elevated event for break-glass (raw shell), normal for helpers
    emit_event(
        connection, tenant,
        "sandbox.breakglass_exec" if break_glass else "sandbox.exec",
        actor, "sandbox", sandbox_id,
        {"command": command, "break_glass": break_glass, "reason": reason},
    )
    return r.json()


def get_stats(settings: Settings, sandbox_id: str) -> dict:
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{_base(settings)}/v1/sandboxes/{sandbox_id}/stats")
    r.raise_for_status()
    return r.json()


def preserve(connection: Connection, settings: Settings, sandbox_id: str, *,
             reason: str, actor: str = "operator") -> dict:
    """Quarantine + snapshot a sandbox for a customer escalation (governed):
    excluded from the reaper, filesystem snapshotted, audited."""
    if not (reason or "").strip():
        raise ValueError("preserve requires a reason")
    tenant = get_sandbox(settings, sandbox_id).get("tenant_id", "unknown")
    with httpx.Client(timeout=180) as c:
        r = c.post(f"{_base(settings)}/v1/sandboxes/{sandbox_id}/preserve",
                   json={"reason": reason, "actor": actor})
    r.raise_for_status()
    emit_event(connection, tenant, "sandbox.preserved", actor, "sandbox", sandbox_id,
               {"reason": reason})
    return r.json()


def destroy(settings: Settings, sandbox_id: str) -> dict:
    with httpx.Client(timeout=30) as c:
        r = c.delete(f"{_base(settings)}/v1/sandboxes/{sandbox_id}")
    r.raise_for_status()
    return r.json()
