"""The owned_resources ledger (stability spec §4a.1): one row per resource BOSS creates.

Written BEFORE the resource exists and closed when it is gone, so the reaper can always answer
"whose is this, and is its owner still alive?" — from the row to the host (an open row whose
owner ended) and from the host back to the row (a labelled container with no open row).

Best effort by default. The reaper's substrate sweep (by label and name prefix) still finds
what an unwritten row would have named. Cleanup owners that must retain retry state until a
release is recorded can opt into strict handle release; its failure propagates to the owner.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import uuid
from typing import Any, Optional

from sqlalchemy import text

_log = logging.getLogger("boss.resource_ledger")


def node_id() -> str:
    """The container (or host) that can see a handle. A pgid or a /tmp path means nothing
    outside the pid/mount namespace that made it; the boot id keeps a restarted container from
    acting on its predecessor's numbers."""
    boot = ""
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="utf-8") as fh:
            boot = fh.read().strip()[:8]
    except OSError:
        pass
    return f"{socket.gethostname()}:{boot or 'noboot'}"


def _owner_fields(owner: Any) -> dict[str, Any]:
    if owner is None:
        return {"kind": "api", "id": "api", "project": None, "chain": []}
    return {"kind": owner.kind, "id": owner.id, "project": owner.project_id,
            "chain": list(owner.chain)}


class DbLedger:
    """The ledger the API turns on (owned_process.set_ledger(DbLedger(engine)))."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.node = node_id()

    def open(self, *, owner: Any, resource_kind: str, handle: str, expires_at: Any = None,
             meta: Optional[dict[str, Any]] = None, pgid: Optional[int] = None) -> Optional[str]:
        row_id = f"own_{uuid.uuid4().hex[:20]}"
        o = _owner_fields(owner)
        try:
            with self.engine.begin() as c:
                c.execute(text(
                    "INSERT INTO owned_resources (id, project_id, owner_kind, owner_id, "
                    "owner_chain, resource_kind, handle, pgid, node, meta, expires_at) VALUES "
                    "(:id, :p, :ok, :oid, :chain, :rk, :h, :pg, :n, CAST(:m AS JSONB), :exp)"),
                    {"id": row_id, "p": o["project"], "ok": o["kind"], "oid": o["id"],
                     "chain": o["chain"], "rk": resource_kind, "h": handle[:500], "pg": pgid,
                     "n": self.node, "m": json.dumps(meta or {}), "exp": expires_at})
        except Exception:  # noqa: BLE001 — see module docstring
            _log.debug("owned_resources open failed", exc_info=True)
            return None
        return row_id

    def set_handle(self, row_id: Optional[str], *, pgid: Optional[int] = None,
                   token: Optional[str] = None) -> None:
        if not row_id:
            return
        try:
            with self.engine.begin() as c:
                c.execute(text("UPDATE owned_resources SET pgid = COALESCE(:pg, pgid) "
                               "WHERE id = :id"), {"id": row_id, "pg": pgid})
        except Exception:  # noqa: BLE001
            _log.debug("owned_resources set_handle failed", exc_info=True)

    def release(self, row_id: Optional[str], reason: str) -> None:
        if not row_id:
            return
        try:
            with self.engine.begin() as c:
                c.execute(text("UPDATE owned_resources SET released_at = now(), "
                               "release_reason = :r WHERE id = :id AND released_at IS NULL"),
                          {"id": row_id, "r": (reason or "")[:200]})
        except Exception:  # noqa: BLE001
            _log.debug("owned_resources release failed", exc_info=True)

    def release_handle(self, resource_kind: str, handle: str, reason: str, *,
                       strict: bool = False) -> int:
        """Close open rows for a handle; strict callers retain their own retry state."""
        try:
            with self.engine.begin() as c:
                return int(c.execute(text(
                    "UPDATE owned_resources SET released_at = now(), release_reason = :r "
                    "WHERE resource_kind = :k AND handle = :h AND released_at IS NULL"),
                    {"k": resource_kind, "h": handle, "r": (reason or "")[:200]}).rowcount or 0)
        except Exception:  # noqa: BLE001
            if strict:
                raise
            _log.debug("owned_resources release_handle failed", exc_info=True)
            return 0

    def open_resources(self, resource_kind: str, handle_prefix: str) -> list[dict[str, Any]]:
        """Read durable retry evidence; a read failure is not an empty ledger.

        Network-resource owners may reconcile a prior process's rows. Their own
        substrate, ownership and liveness checks must precede any row release.
        """
        with self.engine.connect() as c:
            return [dict(row) for row in c.execute(text(
                "SELECT id, owner_kind, owner_id, owner_chain, handle, meta, created_at "
                "FROM owned_resources WHERE resource_kind=:kind AND released_at IS NULL "
                "AND starts_with(handle, :prefix) ORDER BY created_at, id"),
                {"kind": resource_kind, "prefix": handle_prefix}).mappings()]

    def release_owned_row(self, row_id: str, resource_kind: str, handle: str, reason: str) -> int:
        """Strictly close only the inspected row, never a later duplicate handle.

        Exceptions propagate so the cleanup owner reports pending reconciliation.
        Existing best-effort release methods keep their contracts.
        """
        with self.engine.begin() as c:
            return int(c.execute(text(
                "UPDATE owned_resources SET released_at=now(), release_reason=:reason "
                "WHERE id=:id AND resource_kind=:kind AND handle=:handle "
                "AND released_at IS NULL"),
                {"id": row_id, "kind": resource_kind, "handle": handle,
                 "reason": (reason or "")[:200]}).rowcount or 0)


def record(resource_kind: str, handle: str, *, owner: Any = None, expires_at: Any = None,
           meta: Optional[dict[str, Any]] = None) -> Optional[str]:
    """Record a non-process resource (container, image, dir) through whichever ledger is on."""
    from . import owned_process

    led = owned_process.ledger()
    try:
        return led.open(owner=owner or owned_process.current_owner(), resource_kind=resource_kind,
                        handle=handle, expires_at=expires_at, meta=meta)
    except Exception:  # noqa: BLE001
        return None


def release(resource_kind: str, handle: str, reason: str, *, strict: bool = False) -> None:
    """Release a handle, optionally reporting failure to a cleanup owner for retry."""
    from . import owned_process

    led = owned_process.ledger()
    fn = getattr(led, "release_handle", None)
    if fn is not None:
        try:
            if strict and isinstance(led, DbLedger):
                fn(resource_kind, handle, reason, strict=True)
            else:
                fn(resource_kind, handle, reason)
        except Exception:  # noqa: BLE001
            if strict:
                raise


def install_id(connection: Any) -> str:
    """This install's id, created once and kept in system_state. Every container and image
    BOSS makes carries it (`boss.install_id`), so the substrate sweep removes only its own —
    never another install's sharing the Docker daemon (an e2e stack, a second checkout)."""
    env = (os.environ.get("BOSS_INSTALL_ID") or "").strip()
    if env:
        return env
    row = connection.execute(text("SELECT value FROM system_state WHERE key = 'install_id'")
                             ).scalar()
    if isinstance(row, dict) and row.get("id"):
        return str(row["id"])
    if isinstance(row, str):
        try:
            got = json.loads(row).get("id")
            if got:
                return str(got)
        except ValueError:
            pass
    new = f"inst_{uuid.uuid4().hex[:12]}"
    connection.execute(text(
        "INSERT INTO system_state (key, value) VALUES ('install_id', CAST(:v AS JSONB)) "
        "ON CONFLICT (key) DO NOTHING"), {"v": json.dumps({"id": new})})
    again = connection.execute(text("SELECT value FROM system_state WHERE key='install_id'")
                               ).scalar()
    if isinstance(again, dict) and again.get("id"):
        return str(again["id"])
    return new


# ------------------------------------------------------------------------------------------ #
# Directories: claimed while in use, so the reaper's sweep never takes a live one
# ------------------------------------------------------------------------------------------ #

import threading as _threading  # noqa: E402 — kept beside the only code that needs it

_DIRS: dict[str, Optional[str]] = {}
_DIRS_LOCK = _threading.Lock()


def claim_dir(path: Any, *, owner: Any = None) -> None:
    """This directory is in use by `owner` (default: the enclosing owner scope). The reaper
    sweeps BOSS's temp prefixes for directories nobody claims; a claimed one is never touched,
    however old it is (a long build's sandbox)."""
    key = str(path)
    row = record("dir", key, owner=owner)
    with _DIRS_LOCK:
        _DIRS[key] = row


def release_dir(path: Any, reason: str = "removed") -> None:
    key = str(path)
    with _DIRS_LOCK:
        row = _DIRS.pop(key, None)
    from . import owned_process

    if row:
        try:
            owned_process.ledger().release(row, reason)
        except Exception:  # noqa: BLE001
            pass
    else:
        release("dir", key, reason)


def claimed_dirs() -> set[str]:
    with _DIRS_LOCK:
        return set(_DIRS)
