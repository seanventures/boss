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
from .sandbox_provider import (
    MICROVM_CLAIMS_FROZEN_MESSAGE,
    SandboxHostUnavailable,
    SandboxPlaneError,
    ensure_microvm_execution_allowed,
    map_sandbox_http_error,
    microvm_claims_are_frozen,
    sandbox_host_base,
)


def _base(settings: Settings) -> str:
    return sandbox_host_base(settings)


def _auth_headers(settings: Settings) -> dict:
    return {"X-Sandbox-Token": settings.sandbox_token} if settings.sandbox_token else {}


def _require_host(settings: Settings) -> str:
    """Fail closed when the sandbox host URL is missing (503, never 500)."""
    base = _base(settings)
    if not base:
        raise SandboxHostUnavailable(
            "sandbox host not configured (BOSS_SANDBOX_AGENT_URL is empty)"
        )
    return base


def _map_host_error(
    settings: Settings, exc: Exception, *, sandbox_id: Optional[str] = None
) -> SandboxPlaneError:
    """404 / missing host task → 422; other host failures → 503."""
    return map_sandbox_http_error(
        exc,
        base=_base(settings) or "(unset)",
        sandbox_task_id=sandbox_id,
        action="reach",
    )


def list_sandboxes(settings: Settings) -> dict:
    """Report the connected micro-VM execution host. Sean's Firecracker service exposes
    /v1/sandbox/health (VMs are single-tenant, ephemeral & isolated by design — no browse/
    exec API), so we surface the host status as proof the control plane is wired to it.

    Always includes claims_frozen so operators see the GA honesty gate without guessing.
    Unreachable host → 200 with connected=false (not 500).
    """
    base = _base(settings)
    frozen = microvm_claims_are_frozen(settings)
    claims = {
        "claims_frozen": frozen,
        "claims_message": MICROVM_CLAIMS_FROZEN_MESSAGE if frozen else None,
    }
    if not base:
        return {
            "sandboxes": [],
            "host": {"connected": False, **claims},
            "unavailable": "sandbox host not configured (BOSS_SANDBOX_AGENT_URL is empty)",
        }
    try:
        with httpx.Client(timeout=10) as c:
            r = c.get(f"{base}/v1/sandbox/health", headers=_auth_headers(settings))
        r.raise_for_status()
        h = r.json()
        return {
            "sandboxes": [],
            "host": {
                "connected": bool(h.get("ok")),
                "kind": "Firecracker micro-VM",
                "capacity": h.get("capacity"),
                "available": h.get("available"),
                "running": h.get("running"),
                "url": base,
                **claims,
            },
        }
    except Exception as e:  # noqa: BLE001 — fail closed, never 500
        return {
            "sandboxes": [],
            "host": {"connected": False, "url": base, **claims},
            "unavailable": str(e)[:200],
        }


def list_helpers(settings: Settings) -> dict:
    """The governed helper allowlist (no break-glass needed), grouped for display."""
    base = _require_host(settings)
    try:
        with httpx.Client(timeout=10) as c:
            r = c.get(f"{base}/v1/sandbox/helpers", headers=_auth_headers(settings))
        r.raise_for_status()
        return r.json()
    except SandboxPlaneError:
        raise
    except Exception as e:  # noqa: BLE001
        raise _map_host_error(settings, e) from e


# ---- Micro-VM inspection (retained rootfs of a failed/incomplete task) ----------- #
# Sean's service holds a task's VM rootfs ~2h after it ends. These proxy his
# per-task endpoints so the console can root-cause a failure: browse /work/repo,
# view logs, and hold the VM longer. `sandbox_id` here is the sandbox-service task id.
_ROOT = "/work/repo"
_SKIP = {".git", "node_modules", "target", "build", "dist", "__pycache__", ".gradle", ".venv"}


def _fs(settings: Settings, sandbox_id: str, path: str) -> dict:
    base = _require_host(settings)
    try:
        with httpx.Client(timeout=20) as c:
            r = c.get(
                f"{base}/v1/sandbox/tasks/{sandbox_id}/fs",
                params={"path": path},
                headers=_auth_headers(settings),
            )
        r.raise_for_status()
        return r.json()
    except SandboxPlaneError:
        raise
    except Exception as e:  # noqa: BLE001
        raise _map_host_error(settings, e, sandbox_id=sandbox_id) from e


def get_tree(settings: Settings, sandbox_id: str, *, limit: int = 400) -> dict:
    """Breadth-first walk of the retained /work/repo, returned as a flat file list
    (the existing browse UI expects that shape)."""
    # Probe host once so a missing host is a clear 503, not a silent empty tree.
    _require_host(settings)
    out: list[str] = []
    stack = [_ROOT]
    while stack and len(out) < limit:
        d = stack.pop(0)
        try:
            data = _fs(settings, sandbox_id, d)
        except SandboxPlaneError:
            raise
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
    base = _require_host(settings)
    try:
        with httpx.Client(timeout=20) as c:
            r = c.get(
                f"{base}/v1/sandbox/tasks/{sandbox_id}/logs",
                headers=_auth_headers(settings),
            )
        r.raise_for_status()
        return r.json()
    except SandboxPlaneError:
        raise
    except Exception as e:  # noqa: BLE001
        raise _map_host_error(settings, e, sandbox_id=sandbox_id) from e


def hold(connection: Connection, settings: Settings, sandbox_id: str, *,
         hours: int = 2, actor: str = "operator") -> dict:
    """Extend the retention window on a VM so an investigation can continue — audited."""
    base = _require_host(settings)
    try:
        with httpx.Client(timeout=20) as c:
            r = c.post(
                f"{base}/v1/sandbox/tasks/{sandbox_id}/hold",
                json={"hours": hours},
                headers=_auth_headers(settings),
            )
        r.raise_for_status()
        emit_event(
            connection, "sandbox", "sandbox.hold", actor, "sandbox", sandbox_id, {"hours": hours}
        )
        return r.json()
    except SandboxPlaneError:
        raise
    except Exception as e:  # noqa: BLE001
        raise _map_host_error(settings, e, sandbox_id=sandbox_id) from e


def exec_command(connection: Connection, settings: Settings, sandbox_id: str, command: str,
                 *, break_glass: bool = False, reason: Optional[str] = None,
                 actor: str = "operator") -> dict:
    # Execution claims are frozen for Community GA when the flag is set — return a
    # structured 422 rather than pretending live exec works.
    if microvm_claims_are_frozen(settings):
        ensure_microvm_execution_allowed(settings)
    # A retained rootfs is a read-only snapshot — there's no live process to exec into.
    return {"blocked": True,
            "detail": "Live exec isn't available on a retained micro-VM rootfs. Browse the "
                      "files and view the logs to root-cause; use Hold to keep the VM longer."}


def get_stats(settings: Settings, sandbox_id: str) -> dict:
    return {"container_status": "retained rootfs (read-only)", "cpu_pct": None,
            "mem_usage": None, "pids": None}


def destroy(settings: Settings, sandbox_id: str) -> dict:
    """Release a retained VM early (free the slot before the auto-reap)."""
    base = _require_host(settings)
    try:
        with httpx.Client(timeout=20) as c:
            r = c.delete(
                f"{base}/v1/sandbox/tasks/{sandbox_id}",
                headers=_auth_headers(settings),
            )
        r.raise_for_status()
        return r.json() if r.content else {"destroyed": True}
    except SandboxPlaneError:
        raise
    except Exception as e:  # noqa: BLE001
        raise _map_host_error(settings, e, sandbox_id=sandbox_id) from e
