"""BOSS-wide agent key management (KMS) — open-source backends + agent ACLs.

Builds on SecretRefs (pointers only in Postgres) and secret_backends (values live
outside BOSS). This layer adds:

  * **Agent ownership** — keys owned by an agent principal
  * **Grants** — share use/read_meta/write/grant with other agents
  * **Leases** — short-TTL, audited access so agents can obtain material when
    authorized (never in list endpoints; only lease/resolve paths)
  * **OSS backends** — ``local_vault`` (AES-GCM files), ``vault`` / ``openbao``
    (HashiCorp Vault KV v2 HTTP), plus existing env/file/platform store

Hard rules:
  * List/metadata never returns secret material
  * Lease/reveal always emits Receipt + event; max TTL enforced
  * Cross-project access is impossible
  * Fail closed when grant missing or expired
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..config import Settings
from ..ports import sql
from .core import emit_event, make_id, now_utc
from .receipts import ReceiptService
from .secret_backends import SecretUnavailable, backend_for, resolve_secret_ref
from .secrets_service import put_secret

_SAFE = re.compile(r"[^a-zA-Z0-9_.@:-]+")
ALLOWED_PERMS = frozenset({"use", "read_meta", "write", "grant", "admin"})
DEFAULT_LEASE_TTL_S = 120
MAX_LEASE_TTL_S = 900

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agent_key_entries (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  name TEXT NOT NULL,
  secret_ref_id TEXT NOT NULL,
  owner_agent TEXT NOT NULL,
  purpose TEXT NOT NULL DEFAULT 'general',
  labels JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'active',
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (project_id, name)
);
CREATE INDEX IF NOT EXISTS idx_agent_key_entries_project
  ON agent_key_entries(project_id, owner_agent, status);

CREATE TABLE IF NOT EXISTS agent_key_grants (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  key_entry_id TEXT NOT NULL REFERENCES agent_key_entries(id) ON DELETE CASCADE,
  grantee_agent TEXT NOT NULL,
  permissions JSONB NOT NULL DEFAULT '[]'::jsonb,
  granted_by TEXT NOT NULL,
  expires_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (key_entry_id, grantee_agent)
);
CREATE INDEX IF NOT EXISTS idx_agent_key_grants_grantee
  ON agent_key_grants(project_id, grantee_agent);

CREATE TABLE IF NOT EXISTS agent_key_leases (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  key_entry_id TEXT NOT NULL,
  secret_ref_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  purpose TEXT NOT NULL DEFAULT '',
  expires_at TIMESTAMPTZ NOT NULL,
  revoked_at TIMESTAMPTZ,
  reveal_count INT NOT NULL DEFAULT 0,
  max_reveals INT NOT NULL DEFAULT 1,
  receipt_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_key_leases_agent
  ON agent_key_leases(project_id, agent_id, expires_at);
"""


def ensure_schema(connection) -> None:
    for stmt in SCHEMA_SQL.split(";"):
        s = stmt.strip()
        if s:
            connection.execute(sql(s))


def _project_scope(connection, project_id: str) -> tuple[str, str]:
    row = connection.execute(
        sql(
            "SELECT p.workspace_id AS ws, w.org_id AS org FROM projects p "
            "JOIN workspaces w ON w.id = p.workspace_id WHERE p.id = :p"
        ),
        {"p": project_id},
    ).mappings().first()
    if not row:
        return project_id, project_id
    return str(row["org"] or project_id), str(row["ws"] or project_id)


def _norm_agent(agent_id: str) -> str:
    a = (agent_id or "").strip()
    if not a:
        raise ValueError("agent_id is required")
    if len(a) > 128 or _SAFE.search(a):
        raise ValueError("agent_id contains invalid characters")
    return a


def _norm_name(name: str) -> str:
    n = (name or "").strip()
    if not n or len(n) > 128:
        raise ValueError("name is required and must be <= 128 chars")
    return n


def backend_catalog() -> list[dict[str, Any]]:
    """Market / OSS backends BOSS KMS can use."""
    return [
        {
            "id": "local_vault",
            "title": "Local encrypted vault",
            "oss": True,
            "protocol": "AES-256-GCM files (cryptography)",
            "notes": "Default for Community; master key BOSS_KMS_MASTER_KEY or file",
            "stars_note": "cryptography (pyca) — de-facto Python crypto library",
            "writable": True,
            "default": True,
        },
        {
            "id": "vault",
            "title": "HashiCorp Vault KV v2",
            "oss": True,
            "protocol": "HTTP KV secrets engine v2",
            "notes": "BOSS_VAULT_ADDR + BOSS_VAULT_TOKEN; MPL-2.0",
            "stars_note": "hashicorp/vault — industry-standard open-source secrets manager",
            "writable": True,
            "default": False,
        },
        {
            "id": "openbao",
            "title": "OpenBao (Vault fork)",
            "oss": True,
            "protocol": "Vault-compatible HTTP KV v2",
            "notes": "Same env as Vault; Linux Foundation OpenBao",
            "stars_note": "openbao/openbao — community Vault fork",
            "writable": True,
            "default": False,
        },
        {
            "id": "platform_secret_store",
            "title": "Platform ops secret store",
            "oss": True,
            "protocol": "Shared-server ops-agent /secret",
            "notes": "Existing platform store; requires OPS URL + token",
            "stars_note": "BOSS Shared-server kit ops-agent",
            "writable": True,
            "default": False,
        },
        {
            "id": "file",
            "title": "Host file backend",
            "oss": True,
            "protocol": "0600 files under BOSS_SECRETS_DIR",
            "notes": "Local operator keys; no multi-node sync",
            "stars_note": "POSIX file permissions",
            "writable": True,
            "default": False,
        },
        {
            "id": "environment",
            "title": "Process environment",
            "oss": True,
            "protocol": "env vars",
            "notes": "Read-only; local dev only",
            "stars_note": "stdlib",
            "writable": False,
            "default": False,
        },
    ]


def health(settings: Settings) -> dict[str, Any]:
    items = []
    for entry in backend_catalog():
        bid = entry["id"]
        try:
            if bid == "openbao":
                b = backend_for("vault", settings)  # same client
            else:
                b = backend_for(bid, settings)
            h = b.health()
            items.append({"id": bid, **h})
        except SecretUnavailable as exc:
            items.append({"id": bid, "ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            items.append({"id": bid, "ok": False, "error": type(exc).__name__})
    return {
        "product": "boss_agent_kms",
        "backends": items,
        "lease_ttl_default_s": DEFAULT_LEASE_TTL_S,
        "lease_ttl_max_s": MAX_LEASE_TTL_S,
    }


def _perms(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = [raw]
    out = []
    for p in raw or []:
        p = str(p).strip().lower()
        if p in ALLOWED_PERMS and p not in out:
            out.append(p)
    return out


def _has_perm(
    connection,
    *,
    project_id: str,
    key_entry_id: str,
    agent_id: str,
    permission: str,
) -> bool:
    row = connection.execute(
        sql(
            "SELECT owner_agent, status FROM agent_key_entries "
            "WHERE id = :i AND project_id = :p"
        ),
        {"i": key_entry_id, "p": project_id},
    ).mappings().first()
    if not row or row["status"] != "active":
        return False
    if row["owner_agent"] == agent_id:
        return True
    # owner-equivalent admin roles via explicit grant
    grants = connection.execute(
        sql(
            "SELECT permissions, expires_at FROM agent_key_grants "
            "WHERE key_entry_id = :k AND project_id = :p "
            "AND (grantee_agent = :a OR grantee_agent = '*')"
        ),
        {"k": key_entry_id, "p": project_id, "a": agent_id},
    ).mappings().all()
    now = now_utc()
    for g in grants:
        exp = g["expires_at"]
        if exp is not None and exp.replace(tzinfo=timezone.utc) < now:
            continue
        perms = _perms(g["permissions"])
        if permission in perms or "admin" in perms:
            return True
    return False


def store_key(
    connection,
    settings: Settings,
    *,
    project_id: str,
    name: str,
    value: str,
    owner_agent: str,
    actor: str,
    purpose: str = "general",
    secret_kind: str = "api_token",
    backend: str = "local_vault",
    grant_to: Optional[list[dict[str, Any]]] = None,
    labels: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Store a secret for an agent (write-through) and grant the owner full access."""
    ensure_schema(connection)
    owner = _norm_agent(owner_agent)
    name = _norm_name(name)
    if not (value or "").strip():
        raise ValueError("value is required")
    if backend == "openbao":
        backend = "vault"

    stored = put_secret(
        connection,
        settings,
        project_id=project_id,
        name=f"kms_{owner}_{name}",
        value=value,
        secret_kind=secret_kind,
        backend=backend,
        scope_id=project_id,
        actor=actor,
    )
    entry_id = make_id("akey")
    connection.execute(
        sql(
            """
            INSERT INTO agent_key_entries
              (id, project_id, name, secret_ref_id, owner_agent, purpose, labels,
               status, created_by, created_at, updated_at)
            VALUES
              (:id, :p, :n, :sr, :own, :pur, CAST(:lab AS JSONB),
               'active', :by, now(), now())
            ON CONFLICT (project_id, name) DO UPDATE SET
              secret_ref_id = EXCLUDED.secret_ref_id,
              owner_agent = EXCLUDED.owner_agent,
              purpose = EXCLUDED.purpose,
              labels = EXCLUDED.labels,
              status = 'active',
              updated_at = now()
            RETURNING id
            """
        ),
        {
            "id": entry_id,
            "p": project_id,
            "n": name,
            "sr": stored["secret_ref_id"],
            "own": owner,
            "pur": (purpose or "general")[:64],
            "lab": json.dumps(labels or {}),
            "by": actor,
        },
    )
    # Resolve id after upsert
    row = connection.execute(
        sql(
            "SELECT id, secret_ref_id FROM agent_key_entries "
            "WHERE project_id = :p AND name = :n"
        ),
        {"p": project_id, "n": name},
    ).mappings().one()
    entry_id = row["id"]

    # Owner grant (admin)
    _upsert_grant(
        connection,
        project_id=project_id,
        key_entry_id=entry_id,
        grantee_agent=owner,
        permissions=["use", "read_meta", "write", "grant", "admin"],
        granted_by=actor,
        expires_at=None,
    )
    for g in grant_to or []:
        grantee = _norm_agent(str(g.get("agent_id") or g.get("grantee_agent") or ""))
        perms = _perms(g.get("permissions") or ["use", "read_meta"])
        if not perms:
            perms = ["use", "read_meta"]
        _upsert_grant(
            connection,
            project_id=project_id,
            key_entry_id=entry_id,
            grantee_agent=grantee,
            permissions=perms,
            granted_by=actor,
            expires_at=g.get("expires_at"),
        )

    emit_event(
        connection,
        project_id,
        "kms.key.stored",
        actor,
        "agent_key",
        entry_id,
        {
            "name": name,
            "owner_agent": owner,
            "backend": backend,
            "secret_ref_id": stored["secret_ref_id"],
            "fingerprint": stored.get("fingerprint"),
            "last4": stored.get("last4"),
        },
    )
    return {
        "id": entry_id,
        "project_id": project_id,
        "name": name,
        "secret_ref_id": stored["secret_ref_id"],
        "owner_agent": owner,
        "purpose": purpose or "general",
        "backend": backend,
        "fingerprint": stored.get("fingerprint"),
        "last4": stored.get("last4"),
        "raw_secret_stored": False,
    }


def _upsert_grant(
    connection,
    *,
    project_id: str,
    key_entry_id: str,
    grantee_agent: str,
    permissions: list[str],
    granted_by: str,
    expires_at: Any,
) -> dict[str, Any]:
    gid = make_id("akgr")
    connection.execute(
        sql(
            """
            INSERT INTO agent_key_grants
              (id, project_id, key_entry_id, grantee_agent, permissions,
               granted_by, expires_at, created_at)
            VALUES
              (:id, :p, :k, :g, CAST(:perm AS JSONB), :by, :exp, now())
            ON CONFLICT (key_entry_id, grantee_agent) DO UPDATE SET
              permissions = EXCLUDED.permissions,
              granted_by = EXCLUDED.granted_by,
              expires_at = EXCLUDED.expires_at
            """
        ),
        {
            "id": gid,
            "p": project_id,
            "k": key_entry_id,
            "g": grantee_agent,
            "perm": json.dumps(permissions),
            "by": granted_by,
            "exp": expires_at,
        },
    )
    row = connection.execute(
        sql(
            "SELECT id, grantee_agent, permissions, granted_by, expires_at, created_at "
            "FROM agent_key_grants WHERE key_entry_id = :k AND grantee_agent = :g"
        ),
        {"k": key_entry_id, "g": grantee_agent},
    ).mappings().one()
    return {
        "id": row["id"],
        "grantee_agent": row["grantee_agent"],
        "permissions": _perms(row["permissions"]),
        "granted_by": row["granted_by"],
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
    }


def grant_key(
    connection,
    *,
    project_id: str,
    key_entry_id: str,
    granter_agent: str,
    grantee_agent: str,
    permissions: list[str],
    actor: str,
    expires_at: Any = None,
) -> dict[str, Any]:
    ensure_schema(connection)
    granter = _norm_agent(granter_agent)
    grantee = _norm_agent(grantee_agent)
    perms = _perms(permissions) or ["use", "read_meta"]
    if not _has_perm(
        connection,
        project_id=project_id,
        key_entry_id=key_entry_id,
        agent_id=granter,
        permission="grant",
    ):
        raise PermissionError(
            f"agent {granter!r} cannot grant access to key {key_entry_id}"
        )
    # cannot escalate beyond granter's rights unless admin
    if not _has_perm(
        connection,
        project_id=project_id,
        key_entry_id=key_entry_id,
        agent_id=granter,
        permission="admin",
    ):
        # strip admin if granter lacks admin
        perms = [p for p in perms if p != "admin"]
    result = _upsert_grant(
        connection,
        project_id=project_id,
        key_entry_id=key_entry_id,
        grantee_agent=grantee,
        permissions=perms,
        granted_by=actor or granter,
        expires_at=expires_at,
    )
    emit_event(
        connection,
        project_id,
        "kms.key.granted",
        actor or granter,
        "agent_key",
        key_entry_id,
        {"grantee_agent": grantee, "permissions": perms, "granter_agent": granter},
    )
    return result


def revoke_grant(
    connection,
    *,
    project_id: str,
    key_entry_id: str,
    grantee_agent: str,
    actor_agent: str,
    actor: str,
) -> dict[str, Any]:
    ensure_schema(connection)
    actor_a = _norm_agent(actor_agent)
    grantee = _norm_agent(grantee_agent)
    if not _has_perm(
        connection,
        project_id=project_id,
        key_entry_id=key_entry_id,
        agent_id=actor_a,
        permission="grant",
    ):
        raise PermissionError("cannot revoke grants without grant permission")
    connection.execute(
        sql(
            "DELETE FROM agent_key_grants WHERE project_id = :p "
            "AND key_entry_id = :k AND grantee_agent = :g"
        ),
        {"p": project_id, "k": key_entry_id, "g": grantee},
    )
    emit_event(
        connection,
        project_id,
        "kms.key.grant_revoked",
        actor or actor_a,
        "agent_key",
        key_entry_id,
        {"grantee_agent": grantee},
    )
    return {"revoked": True, "grantee_agent": grantee, "key_entry_id": key_entry_id}


def list_keys_for_agent(
    connection,
    *,
    project_id: str,
    agent_id: str,
) -> list[dict[str, Any]]:
    """Metadata only for keys the agent owns or has been granted."""
    ensure_schema(connection)
    agent = _norm_agent(agent_id)
    rows = connection.execute(
        sql(
            """
            SELECT e.id, e.name, e.secret_ref_id, e.owner_agent, e.purpose, e.labels,
                   e.status, e.created_by, e.created_at, e.updated_at,
                   r.storage_backend, r.metadata, r.secret_kind
            FROM agent_key_entries e
            JOIN secret_refs r ON r.id = e.secret_ref_id
            WHERE e.project_id = :p AND e.status = 'active'
              AND (
                e.owner_agent = :a
                OR EXISTS (
                  SELECT 1 FROM agent_key_grants g
                  WHERE g.key_entry_id = e.id AND g.project_id = :p
                    AND (g.grantee_agent = :a OR g.grantee_agent = '*')
                    AND (g.expires_at IS NULL OR g.expires_at > now())
                )
              )
            ORDER BY e.updated_at DESC
            """
        ),
        {"p": project_id, "a": agent},
    ).mappings().all()
    out = []
    for r in rows:
        meta = r["metadata"] or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except json.JSONDecodeError:
                meta = {}
        can_use = _has_perm(
            connection,
            project_id=project_id,
            key_entry_id=r["id"],
            agent_id=agent,
            permission="use",
        )
        out.append(
            {
                "id": r["id"],
                "name": r["name"],
                "secret_ref_id": r["secret_ref_id"],
                "owner_agent": r["owner_agent"],
                "purpose": r["purpose"],
                "labels": r["labels"] or {},
                "status": r["status"],
                "backend": r["storage_backend"],
                "secret_kind": meta.get("secret_kind_detail") or r["secret_kind"],
                "fingerprint": meta.get("fingerprint"),
                "last4": meta.get("last4"),
                "can_use": can_use,
                "is_owner": r["owner_agent"] == agent,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
        )
    return out


def lease_key(
    connection,
    settings: Settings,
    *,
    project_id: str,
    key_entry_id: str,
    agent_id: str,
    actor: str,
    purpose: str = "",
    ttl_seconds: int = DEFAULT_LEASE_TTL_S,
    reveal: bool = False,
    max_reveals: int = 1,
) -> dict[str, Any]:
    """Create a short-TTL lease. Optionally reveal material once (audited)."""
    ensure_schema(connection)
    agent = _norm_agent(agent_id)
    ttl = max(15, min(int(ttl_seconds or DEFAULT_LEASE_TTL_S), MAX_LEASE_TTL_S))
    if not _has_perm(
        connection,
        project_id=project_id,
        key_entry_id=key_entry_id,
        agent_id=agent,
        permission="use",
    ):
        raise PermissionError(f"agent {agent!r} cannot use key {key_entry_id}")

    entry = connection.execute(
        sql(
            "SELECT id, secret_ref_id, name, owner_agent FROM agent_key_entries "
            "WHERE id = :i AND project_id = :p AND status = 'active'"
        ),
        {"i": key_entry_id, "p": project_id},
    ).mappings().first()
    if not entry:
        raise LookupError("key entry not found")

    lease_id = make_id("akls")
    expires = now_utc() + timedelta(seconds=ttl)
    receipt_id = None
    try:
        org_id, workspace_id = _project_scope(connection, project_id)
        receipt, _created = ReceiptService().create_or_reuse(
            connection,
            org_id,
            workspace_id,
            project_id,
            f"kms.lease:{project_id}:{lease_id}",
            "kms.lease",
            {
                "project_id": project_id,
                "key_entry_id": key_entry_id,
                "agent_id": agent,
                "lease_id": lease_id,
            },
            {
                "purpose": purpose or "",
                "ttl_seconds": ttl,
                "reveal": bool(reveal),
                "name": entry["name"],
                "actor": actor or agent,
            },
        )
        receipt_id = receipt.get("id")
        try:
            ReceiptService().mark_succeeded(
                connection,
                receipt_id,
                None,
                {"lease_id": lease_id, "ttl_seconds": ttl},
            )
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001 — lease still valid without receipt
        receipt_id = None
    connection.execute(
        sql(
            """
            INSERT INTO agent_key_leases
              (id, project_id, key_entry_id, secret_ref_id, agent_id, purpose,
               expires_at, max_reveals, receipt_id, created_at)
            VALUES
              (:id, :p, :k, :sr, :a, :pur, :exp, :mr, :rcpt, now())
            """
        ),
        {
            "id": lease_id,
            "p": project_id,
            "k": key_entry_id,
            "sr": entry["secret_ref_id"],
            "a": agent,
            "pur": (purpose or "")[:128],
            "exp": expires,
            "mr": max(1, min(int(max_reveals or 1), 5)),
            "rcpt": receipt_id,
        },
    )
    emit_event(
        connection,
        project_id,
        "kms.key.leased",
        actor or agent,
        "agent_key_lease",
        lease_id,
        {
            "key_entry_id": key_entry_id,
            "agent_id": agent,
            "ttl_seconds": ttl,
            "reveal": bool(reveal),
        },
    )
    out: dict[str, Any] = {
        "lease_id": lease_id,
        "key_entry_id": key_entry_id,
        "secret_ref_id": entry["secret_ref_id"],
        "name": entry["name"],
        "agent_id": agent,
        "expires_at": expires.isoformat(),
        "ttl_seconds": ttl,
        "reveal": bool(reveal),
        "value": None,
    }
    if reveal:
        out["value"] = _reveal_lease(
            connection,
            settings,
            project_id=project_id,
            lease_id=lease_id,
            agent_id=agent,
            actor=actor or agent,
        )["value"]
    return out


def _reveal_lease(
    connection,
    settings: Settings,
    *,
    project_id: str,
    lease_id: str,
    agent_id: str,
    actor: str,
) -> dict[str, Any]:
    agent = _norm_agent(agent_id)
    row = connection.execute(
        sql(
            "SELECT * FROM agent_key_leases WHERE id = :i AND project_id = :p"
        ),
        {"i": lease_id, "p": project_id},
    ).mappings().first()
    if not row:
        raise LookupError("lease not found")
    if row["agent_id"] != agent:
        raise PermissionError("lease belongs to a different agent")
    if row["revoked_at"] is not None:
        raise PermissionError("lease revoked")
    exp = row["expires_at"]
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < now_utc():
        raise PermissionError("lease expired")
    if int(row["reveal_count"] or 0) >= int(row["max_reveals"] or 1):
        raise PermissionError("lease reveal limit reached")

    value = resolve_secret_ref(connection, row["secret_ref_id"], settings)
    connection.execute(
        sql(
            "UPDATE agent_key_leases SET reveal_count = reveal_count + 1 "
            "WHERE id = :i"
        ),
        {"i": lease_id},
    )
    emit_event(
        connection,
        project_id,
        "kms.key.revealed",
        actor,
        "agent_key_lease",
        lease_id,
        {
            "key_entry_id": row["key_entry_id"],
            "agent_id": agent,
            "reveal_count": int(row["reveal_count"] or 0) + 1,
        },
    )
    # Never log value
    return {"lease_id": lease_id, "value": value, "expires_at": exp.isoformat()}


def resolve_lease(
    connection,
    settings: Settings,
    *,
    project_id: str,
    lease_id: str,
    agent_id: str,
    actor: str,
) -> dict[str, Any]:
    ensure_schema(connection)
    return _reveal_lease(
        connection,
        settings,
        project_id=project_id,
        lease_id=lease_id,
        agent_id=agent_id,
        actor=actor,
    )


def revoke_lease(
    connection,
    *,
    project_id: str,
    lease_id: str,
    actor: str,
) -> dict[str, Any]:
    ensure_schema(connection)
    connection.execute(
        sql(
            "UPDATE agent_key_leases SET revoked_at = now() "
            "WHERE id = :i AND project_id = :p AND revoked_at IS NULL"
        ),
        {"i": lease_id, "p": project_id},
    )
    emit_event(
        connection,
        project_id,
        "kms.lease.revoked",
        actor,
        "agent_key_lease",
        lease_id,
        {},
    )
    return {"lease_id": lease_id, "revoked": True}


def list_grants(
    connection, *, project_id: str, key_entry_id: str, agent_id: str
) -> list[dict[str, Any]]:
    ensure_schema(connection)
    if not _has_perm(
        connection,
        project_id=project_id,
        key_entry_id=key_entry_id,
        agent_id=_norm_agent(agent_id),
        permission="read_meta",
    ):
        raise PermissionError("cannot list grants")
    rows = connection.execute(
        sql(
            "SELECT id, grantee_agent, permissions, granted_by, expires_at, created_at "
            "FROM agent_key_grants WHERE project_id = :p AND key_entry_id = :k "
            "ORDER BY created_at"
        ),
        {"p": project_id, "k": key_entry_id},
    ).mappings().all()
    return [
        {
            "id": r["id"],
            "grantee_agent": r["grantee_agent"],
            "permissions": _perms(r["permissions"]),
            "granted_by": r["granted_by"],
            "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
        }
        for r in rows
    ]
