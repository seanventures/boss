"""The guest's `boss_egr_` handle on the microVM lane (P8b S11, BOSSAPP-1072; boss-agent-hosts-lld.md
§3.8, §3.11; HOST_API.md §4.9).

The local lane's egress credential (runs.issue_egress_credential) is bound to the sandbox's
network address and presented by git straight to boss-egress. A microVM has no network: its git
reaches egress only through `boss-hostd`, which identifies the VM by the vsock socket the call
arrived on, moves the handle into `X-Boss-Handle`, and adds its own proof, its word on VM, run and
epoch, and the run's `boss:egress` token. So a VM handle:

  * lives in the same table (identity_egress_credentials: hash only, ≤ 1 h, revoked at run end)
    with `sandbox_id = 'host:'` — a mark `runs.resolve_egress` refuses, so the handle is worthless
    when presented to egress directly (`egress_host_route_only`);
  * is bound to (org, run, epoch, vm). A handle minted before its VM existed is bound to the first
    VM that redeems it; from then on another VM, run or org presenting it is a REPLAY (revoked,
    `host_events(handle_replay, actor=egress)`, refused);
  * carries no authority of its own: what the run may do in git comes from its `boss:egress` run
    token (repos, permissions, push pattern), never from the handle.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

PREFIX = "boss_egr_"
#: identity_egress_credentials.sandbox_id of a handle that is redeemed only through a host.
HOST_ROUTE_MARK = "host:"
TTL = timedelta(hours=1)


def hash_handle(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def fingerprint(raw: str) -> str:
    return hash_handle(raw)[:16]


@dataclass
class VMHandle:
    token_hash: str
    org_id: Optional[str]
    run_id: str
    fence_epoch: int
    vm_id: Optional[str]
    deployment_id: Optional[str]
    expires_at: Optional[datetime]
    revoked_at: Optional[datetime]

    def live(self) -> bool:
        return self.revoked_at is None and (self.expires_at is None
                                            or self.expires_at > datetime.now(timezone.utc))


def issue(connection: Any, *, org_id: str, run_id: str, epoch: int, vm_id: Optional[str],
          deployment_id: Optional[str] = None, ttl: timedelta = TTL) -> str:
    """Mint a VM handle for the run. Returns the raw handle ONCE (only its hash is stored; the
    value is registered with the run redactor)."""
    from sqlalchemy import text

    from .ids import new_id

    raw = PREFIX + secrets.token_urlsafe(32)
    connection.execute(text(
        "INSERT INTO identity_egress_credentials (id, token_hash, run_id, sandbox_id, org_id, "
        "vm_id, fence_epoch, deployment_id, expires_at) VALUES (:id, :h, :r, :s, :o, :v, :e, :d, "
        ":x)"),
        {"id": new_id("egr"), "h": hash_handle(raw), "r": run_id, "s": HOST_ROUTE_MARK,
         "o": org_id, "v": vm_id, "e": int(epoch), "d": deployment_id,
         "x": datetime.now(timezone.utc) + ttl})
    try:
        from .redaction import register_secrets

        register_secrets([raw])
    except Exception:  # noqa: BLE001 — redaction is defence in depth
        pass
    return raw


def load(connection: Any, raw: str) -> Optional[VMHandle]:
    """The VM handle's row, or None (unknown, malformed, or a local-lane credential)."""
    from sqlalchemy import text

    if not raw or not raw.startswith(PREFIX) or len(raw) > 128:
        return None
    row = connection.execute(text(
        "SELECT token_hash, org_id, run_id, fence_epoch, vm_id, deployment_id, expires_at, "
        "revoked_at, sandbox_id FROM identity_egress_credentials WHERE token_hash = :h"),
        {"h": hash_handle(raw)}).mappings().first()
    if row is None or str(row["sandbox_id"] or "") != HOST_ROUTE_MARK:
        return None
    return VMHandle(token_hash=row["token_hash"], org_id=row["org_id"], run_id=row["run_id"],
                    fence_epoch=int(row["fence_epoch"] or 0), vm_id=row["vm_id"],
                    deployment_id=row["deployment_id"], expires_at=row["expires_at"],
                    revoked_at=row["revoked_at"])


def bind_vm(connection: Any, token_hash: str, vm_id: str) -> str:
    """Bind an unbound handle to the VM that first redeems it; returns the VM it is bound to
    (another VM's, if a race bound it first — the caller then treats that as a replay)."""
    from sqlalchemy import text

    connection.execute(text(
        "UPDATE identity_egress_credentials SET vm_id = :v WHERE token_hash = :h "
        "AND vm_id IS NULL"), {"h": token_hash, "v": vm_id})
    return str(connection.execute(text(
        "SELECT vm_id FROM identity_egress_credentials WHERE token_hash = :h"),
        {"h": token_hash}).scalar() or vm_id)


def revoke(connection: Any, token_hash: str, reason: str) -> None:
    from sqlalchemy import text

    connection.execute(text(
        "UPDATE identity_egress_credentials SET revoked_at = now(), revoked_reason = :r "
        "WHERE token_hash = :h AND revoked_at IS NULL"), {"h": token_hash, "r": reason[:200]})


def revoke_run(connection: Any, run_id: str, reason: str = "run ended") -> int:
    """Every VM handle of a run (and its children) dies with its lease; returns how many."""
    from sqlalchemy import text

    with connection.begin_nested():
        result = connection.execute(text(
            "UPDATE identity_egress_credentials SET revoked_at = now(), revoked_reason = :re "
            "WHERE (run_id = :r OR run_id LIKE :c) AND sandbox_id = :s AND revoked_at IS NULL"),
            {"r": run_id, "c": run_id + "/%", "s": HOST_ROUTE_MARK, "re": reason[:200]})
        return int(result.rowcount or 0)


__all__ = ["HOST_ROUTE_MARK", "PREFIX", "VMHandle", "bind_vm", "fingerprint", "hash_handle",
           "issue", "load", "revoke", "revoke_run"]
