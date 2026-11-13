"""A persistent signing key per agent (BOSSAPP-859).

An agent's id is stable (agt_…), its Passport and run tokens are short-lived, and none of that
lets a THIRD PARTY check, without calling BOSS, that an output came from a given agent. So an
agent may hold one Ed25519 key of its own:

  * agent_keys(project_id, role_key, agent_id, kid, public_jwk, public_pem, private_vault_item_id)
    — the private half is a vault item (kind signing_key) and NEVER leaves BOSS: nothing returns
    it, and the agent signs through BOSS (the `sign` tool on the run's boss_run gateway server);
  * GET /v1/agents/{agent_id}/jwks publishes the agent's public keys: the active one and any it
    retired (still good for what they signed). A REVOKED key (compromised) is not published;
  * kid = the RFC 7638 thumbprint of the public JWK.

WHAT IS SIGNED (boss.agent-sig/v1). A detached Ed25519 signature over

    b"boss.agent-sig/v1\\n" + purpose + b"\\n" + payload

so a signature made for one purpose (`commit`, `pr-evidence`, `output`) can never be replayed as
another's. `verify` is the reference check; the same few lines in any language verify it from
the JWKS alone.

  * commits: every commit BOSS makes inside a build whose lead agent holds a key ends with
    `BOSS-Agent-Signature: <agent_id> <kid> <sig>` over the message above that line (rstripped);
  * the PR: `BOSS-Agent-Key:` and `BOSS-Agent-Evidence:` (a signature over the canonical JSON
    {run, agent, role, act}) in the PR's trailer block.

No key is ever created implicitly: an agent without one signs nothing and every commit and PR is
byte-identical to before. A key is created (or rotated) by a person, on the agent's page.
"""
from __future__ import annotations

import base64
import json
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import text
from sqlalchemy.engine import Connection

DOMAIN = b"boss.agent-sig/v1"
PURPOSES = ("commit", "pr-evidence", "output")
COMMIT_TRAILER = "BOSS-Agent-Signature:"
MAX_PAYLOAD = 256 * 1024

_lock = threading.Lock()
_private_cache: dict[str, Ed25519PrivateKey] = {}


class AgentKeyError(ValueError):
    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def message_for(purpose: str, payload: bytes | str) -> bytes:
    if purpose not in PURPOSES:
        raise AgentKeyError(f"purpose must be one of {list(PURPOSES)}")
    raw = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    if len(raw) > MAX_PAYLOAD:
        raise AgentKeyError(f"payload is larger than {MAX_PAYLOAD} bytes")
    return DOMAIN + b"\n" + purpose.encode("ascii") + b"\n" + raw


def _agent(connection: Connection, project_id: str, role_key: str) -> dict[str, Any]:
    row = connection.execute(
        text("SELECT r.role_key, r.agent_id, w.org_id FROM roles r "
             "JOIN projects p ON p.id = r.project_id JOIN workspaces w ON w.id = p.workspace_id "
             "WHERE r.project_id = :p AND r.role_key = :r"),
        {"p": project_id, "r": role_key}).mappings().first()
    if row is None:
        raise AgentKeyError(f"no agent {role_key!r} in this project", status=404)
    if not row["agent_id"]:
        raise AgentKeyError(f"{role_key} has no agent id yet, so it cannot hold a key", 409)
    return dict(row)


def _view(row: Any) -> dict[str, Any]:
    return {"kid": row["kid"], "agent_id": row["agent_id"], "role_key": row["role_key"],
            "alg": "EdDSA", "status": row["status"], "public_jwk": row["public_jwk"],
            "public_pem": row["public_pem"],
            "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
            "retired_at": row["retired_at"].isoformat() if row.get("retired_at") else None}


_COLS = ("kid, project_id, role_key, agent_id, public_jwk, public_pem, private_vault_item_id, "
         "status, created_by, created_at, retired_at")


def create_key(connection: Connection, project_id: str, role_key: str, *, actor: str,
               rotate: bool = False) -> dict[str, Any]:
    """Give the agent its key. With one already active: refused, unless `rotate`, which retires
    it (still published, still verifies what it signed) and makes a new one active."""
    from ..identity import jwks, vault

    agent = _agent(connection, project_id, role_key)
    connection.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                       {"k": f"agent_key:{agent['agent_id']}"})
    active = connection.execute(
        text("SELECT kid FROM agent_keys WHERE agent_id = :a AND status = 'active'"),
        {"a": agent["agent_id"]}).scalar()
    if active and not rotate:
        raise AgentKeyError(f"{role_key} already holds key {active}; rotate it instead", 409)
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    jwk = jwks.public_jwk(public)
    kid = jwks.thumbprint(jwk)
    jwk = {**jwk, "kid": kid, "alg": "EdDSA", "use": "sig"}
    pem_private = private.private_bytes(serialization.Encoding.PEM,
                                        serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()).decode("ascii")
    pem_public = public.public_bytes(serialization.Encoding.PEM,
                                     serialization.PublicFormat.SubjectPublicKeyInfo
                                     ).decode("ascii")
    item = vault.store(org_id=agent["org_id"], owner_principal_id=None,
                       kind=vault.VaultItemKind.SIGNING_KEY, provider="boss-agent",
                       secret=pem_private, db=connection,
                       connection_name=f"agent-key:{agent['agent_id']}")
    if active:
        connection.execute(
            text("UPDATE agent_keys SET status = 'retired', retired_at = now() WHERE kid = :k"),
            {"k": active})
    row = connection.execute(
        text(f"INSERT INTO agent_keys ({_COLS}) VALUES (:kid, :p, :r, :a, CAST(:jwk AS JSONB), "
             f":pem, :item, 'active', :by, now(), NULL) RETURNING {_COLS}"),
        {"kid": kid, "p": project_id, "r": role_key, "a": agent["agent_id"],
         "jwk": json.dumps(jwk, sort_keys=True), "pem": pem_public, "item": item,
         "by": actor}).mappings().one()
    with _lock:
        _private_cache[kid] = private
    from .core import emit_event

    emit_event(connection, project_id, "agent.key_rotated" if active else "agent.key_created",
               actor, "agent", role_key, {"kid": kid, "agent_id": agent["agent_id"],
                                          "retired": active})
    return _view(row)


def revoke_key(connection: Connection, project_id: str, role_key: str, kid: str, *,
               actor: str, reason: str = "") -> dict[str, Any]:
    """A compromised key: no longer published, never signs again."""
    row = connection.execute(
        text(f"UPDATE agent_keys SET status = 'revoked', retired_at = COALESCE(retired_at, now()) "
             f"WHERE project_id = :p AND role_key = :r AND kid = :k AND status <> 'revoked' "
             f"RETURNING {_COLS}"),
        {"p": project_id, "r": role_key, "k": kid}).mappings().first()
    if row is None:
        raise AgentKeyError(f"no live key {kid} on {role_key}", 404)
    with _lock:
        _private_cache.pop(kid, None)
    from .core import emit_event

    emit_event(connection, project_id, "agent.key_revoked", actor, "agent", role_key,
               {"kid": kid, "reason": reason[:400]})
    return _view(row)


def keys_for(connection: Connection, project_id: str, role_key: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(f"SELECT {_COLS} FROM agent_keys WHERE project_id = :p AND role_key = :r "
             "ORDER BY created_at DESC"), {"p": project_id, "r": role_key}).mappings().all()
    return [_view(r) for r in rows]


def jwks_for_agent(connection: Connection, agent_id: str) -> Optional[dict[str, Any]]:
    """The agent's published keys (active + retired, never revoked), or None for an unknown id."""
    known = connection.execute(text("SELECT 1 FROM roles WHERE agent_id = :a LIMIT 1"),
                               {"a": agent_id}).first()
    if known is None:
        return None
    rows = connection.execute(
        text("SELECT public_jwk, status FROM agent_keys WHERE agent_id = :a "
             "AND status IN ('active', 'retired') ORDER BY (status = 'active') DESC, created_at DESC"),
        {"a": agent_id}).mappings().all()
    keys = []
    for r in rows:
        jwk = r["public_jwk"] if isinstance(r["public_jwk"], dict) else json.loads(r["public_jwk"])
        keys.append({**jwk, "status": r["status"]})
    return {"agent_id": agent_id, "keys": keys}


def has_active_key(connection: Connection, project_id: str, role_key: Optional[str]) -> bool:
    if not role_key:
        return False
    return connection.execute(
        text("SELECT 1 FROM agent_keys WHERE project_id = :p AND role_key = :r "
             "AND status = 'active'"), {"p": project_id, "r": role_key}).first() is not None


def _private_for(connection: Connection, kid: str, item_id: str) -> Ed25519PrivateKey:
    from ..identity import vault

    with _lock:
        cached = _private_cache.get(kid)
    if cached is not None:
        return cached
    pem = vault.read_secret(item_id, db=connection, kind=vault.VaultItemKind.SIGNING_KEY)
    key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise AgentKeyError("the agent's key is not Ed25519", 500)
    with _lock:
        _private_cache[kid] = key
    return key


def sign(connection: Connection, project_id: str, role_key: str, payload: bytes | str, *,
         purpose: str = "output") -> dict[str, Any]:
    """Sign `payload` with the agent's active key, inside BOSS. Returns the detached signature
    and what a verifier needs to find the key — never the key."""
    message = message_for(purpose, payload)
    row = connection.execute(
        text("SELECT kid, agent_id, private_vault_item_id FROM agent_keys WHERE project_id = :p "
             "AND role_key = :r AND status = 'active'"),
        {"p": project_id, "r": role_key}).mappings().first()
    if row is None:
        raise AgentKeyError(f"{role_key} holds no signing key", 409)
    signature = _private_for(connection, row["kid"], row["private_vault_item_id"]).sign(message)
    return {"agent_id": row["agent_id"], "kid": row["kid"], "alg": "EdDSA",
            "purpose": purpose, "signature": _b64u(signature),
            "signed_at": datetime.now(timezone.utc).isoformat(),
            "jwks": f"/v1/agents/{row['agent_id']}/jwks"}


def verify(public_jwk: dict[str, Any], payload: bytes | str, signature: str, *,
           purpose: str) -> bool:
    """The reference check: the JWKS entry, the payload, the signature, the purpose."""
    from ..identity import jwks

    try:
        jwks.public_key_from_jwk(public_jwk).verify(_b64u_decode(signature),
                                                    message_for(purpose, payload))
        return True
    except (InvalidSignature, ValueError, Exception):  # noqa: BLE001 — a bad JWK is a "no"
        return False


# ── commits and the PR ────────────────────────────────────────────────────────────────────────

def signer_for_run(connection: Connection, code_task_run_id: str
                   ) -> Optional[tuple[str, str]]:
    """(project_id, role_key) of the build's lead agent when it holds an active key, else None."""
    from .agent_acts import trailers

    task = connection.execute(text("SELECT project_id FROM code_task_runs WHERE id = :r"),
                              {"r": code_task_run_id}).first()
    if task is None:
        return None
    made = trailers(connection, code_task_run_id) or {}
    lead = (made.get("lead") or {}).get("role_key")
    if lead and has_active_key(connection, task[0], lead):
        return task[0], lead
    return None


def sign_commit(connection: Connection, message: str, signer: tuple[str, str]) -> str:
    """`message` with a `BOSS-Agent-Signature:` trailer over everything above it."""
    body = message.rstrip()
    signed = sign(connection, signer[0], signer[1], body, purpose="commit")
    sep = "\n" if _ends_in_trailers(body) else "\n\n"
    return f"{body}{sep}{COMMIT_TRAILER} {signed['agent_id']} {signed['kid']} {signed['signature']}"


def _ends_in_trailers(body: str) -> bool:
    last = body.rsplit("\n\n", 1)[-1]
    return bool(last) and all(":" in line for line in last.splitlines())


def split_signed_commit(message: str) -> Optional[tuple[str, str, str, str]]:
    """(body, agent_id, kid, signature) of a signed commit message, else None."""
    lines = message.rstrip().splitlines()
    if not lines or not lines[-1].startswith(COMMIT_TRAILER):
        return None
    parts = lines[-1][len(COMMIT_TRAILER):].split()
    if len(parts) != 3:
        return None
    body = "\n".join(lines[:-1]).rstrip()
    return body, parts[0], parts[1], parts[2]


def pr_evidence(connection: Connection, code_task_run_id: str, *, agent_id: Optional[str] = None,
                role_key: Optional[str] = None, act_id: Optional[str] = None) -> str:
    """The PR's signed evidence lines, or "" when the lead agent holds no key."""
    signer = signer_for_run(connection, code_task_run_id)
    if signer is None:
        return ""
    statement = json.dumps({"run": code_task_run_id, "agent": agent_id, "role": signer[1],
                            "act": act_id}, sort_keys=True, separators=(",", ":"))
    signed = sign(connection, signer[0], signer[1], statement, purpose="pr-evidence")
    return (f"BOSS-Agent-Key: {signed['agent_id']} {signed['kid']} ({signed['jwks']})\n"
            f"BOSS-Agent-Evidence: {_b64u(statement.encode('utf-8'))}.{signed['signature']}")


def clear_cache_for_tests() -> None:
    with _lock:
        _private_cache.clear()
