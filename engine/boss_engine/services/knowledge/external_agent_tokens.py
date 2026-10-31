"""KG P6a: the per-execution Knowledge-Graph token a bank's external agent is handed.

No DPoP, no `identity/tokens.py` run-token chain (tech plan, "Resolved ambiguities" #1): BOSS
hands this credential to a remote party it does not run in-process or in a sandbox it
controls, so a plain, opaque, server-minted bearer — short TTL, checked against a live
revocation record and a stored scope on every call — is the honest posture for a one-shot
handoff. This is new, narrow surface, not an extension of the DPoP run-token system.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import scopes

log = logging.getLogger(__name__)

#: However long a step's timeout asks for, a token never outlives this (a crashed step must not
#: strand a bearer credential indefinitely). The `external_agent_kg_tokens` table itself is
#: created by `db/schema.sql`'s own migration, not by a request-path `ensure_schema()`.
HARD_CAP_SECONDS = 3600

_TOKEN_PREFIX = "boss_ext_"


def _audit(connection: Connection, project_id: str, event_type: str, *, token_id: str,
            external_agent_id: str = "", **details) -> None:
    """The hash-chained identity audit for a token's mint/revoke — never raises; a failure to
    write it logs a warning and never changes whether the mint/revoke itself succeeded (the
    ``self_fix.audit()`` convention: an audit record, not a gate)."""
    try:
        from ...identity import audit as identity_audit
    except Exception:  # noqa: BLE001
        return
    try:
        org_id = connection.execute(text(
            "SELECT o.id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
            "JOIN orgs o ON o.id = w.org_id WHERE p.id = :p"
        ), {"p": project_id}).scalar()
        if not org_id:
            return
        with connection.begin_nested():
            identity_audit.record(identity_audit.SecurityEvent(
                org_id=org_id, event_type=event_type, outcome=identity_audit.Outcome.SUCCESS,
                actor_kind="agent", actor_principal_id=f"external_agent:{external_agent_id}",
                target_type="external_agent_kg_token", target_id=token_id,
                details=dict(details)), db=connection)
    except Exception as error:  # noqa: BLE001
        log.warning("external agent token audit %s not written: %s", event_type, error)


@dataclass(frozen=True)
class IssuedKgToken:
    token: str
    token_id: str
    expires_at: "object"


@dataclass(frozen=True)
class KgTokenClaims:
    project_id: str
    run_id: str
    step_id: str
    external_agent_id: str
    policy: scopes.StagePolicy


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _policy_to_json(policy: scopes.StagePolicy) -> dict:
    return {
        "stage": policy.stage,
        "layers": sorted(policy.layers),
        "code": policy.code,
        "max_hops": policy.max_hops,
        "max_nodes": policy.max_nodes,
        "budget_tokens": policy.budget_tokens,
        "tools": sorted(policy.tools) if policy.tools is not None else None,
        "seeds": list(policy.seeds),
        "kinds": sorted(policy.kinds) if policy.kinds is not None else None,
    }


def _policy_from_json(raw: dict) -> scopes.StagePolicy:
    return scopes.StagePolicy(
        stage=raw.get("stage") or "custom",
        layers=frozenset(raw.get("layers") or ()),
        code=raw.get("code") or "none",
        max_hops=int(raw.get("max_hops") or 0),
        max_nodes=int(raw.get("max_nodes") or 0),
        budget_tokens=int(raw.get("budget_tokens") or 1200),
        tools=(frozenset(raw["tools"]) if raw.get("tools") is not None else None),
        seeds=tuple(raw.get("seeds") or ()),
        kinds=(frozenset(raw["kinds"]) if raw.get("kinds") is not None else None),
    )


def mint(connection: Connection, *, project_id: str, run_id: str, step_id: str,
          external_agent_id: str, policy: scopes.StagePolicy, ttl_seconds: int) -> IssuedKgToken:
    """A fresh, opaque bearer token scoped to exactly ``policy`` for this one execution."""
    token = _TOKEN_PREFIX + secrets.token_urlsafe(32)
    token_id = f"extkgt_{uuid.uuid4().hex[:20]}"
    ttl = max(1, min(int(ttl_seconds or 0) or HARD_CAP_SECONDS, HARD_CAP_SECONDS))
    row = connection.execute(text(
        "INSERT INTO external_agent_kg_tokens "
        "(id, project_id, run_id, step_id, external_agent_id, token_hash, policy, expires_at) "
        "VALUES (:id, :p, :r, :s, :a, :h, :policy, now() + (:ttl || ' seconds')::interval) "
        "RETURNING expires_at"
    ), {"id": token_id, "p": project_id, "r": run_id, "s": step_id, "a": external_agent_id,
        "h": _hash(token), "policy": json.dumps(_policy_to_json(policy)), "ttl": ttl}).first()
    _audit(connection, project_id, "external_agent.token_minted", token_id=token_id,
           external_agent_id=external_agent_id, run_id=run_id, step_id=step_id)
    return IssuedKgToken(token=token, token_id=token_id, expires_at=row[0])


def verify(connection: Connection, token: str) -> Optional[KgTokenClaims]:
    """The token's claims, live — a token that is revoked, expired, or simply unknown is None,
    checked fresh against the stored record every call, never cached by the caller."""
    if not token or not token.startswith(_TOKEN_PREFIX):
        return None
    # Fail closed on a connection that is no longer usable: there is nothing to verify against, so
    # the token is simply not valid for this call (never raise out of a verify).
    if getattr(connection, "closed", False):
        return None
    row = connection.execute(text(
        "SELECT project_id, run_id, step_id, external_agent_id, policy FROM external_agent_kg_tokens "
        "WHERE token_hash = :h AND revoked_at IS NULL AND expires_at > now()"
    ), {"h": _hash(token)}).mappings().first()
    if not row:
        return None
    raw_policy = row["policy"]
    if isinstance(raw_policy, str):
        raw_policy = json.loads(raw_policy)
    return KgTokenClaims(
        project_id=row["project_id"], run_id=row["run_id"], step_id=row["step_id"],
        external_agent_id=row["external_agent_id"], policy=_policy_from_json(raw_policy))


def revoke_all_for_agent(connection: Connection, project_id: str, external_agent_id: str) -> int:
    """Suspend/remove's fail-closed guarantee: every still-live token this agent holds is
    revoked right away, not just left to expire, so a run already in flight loses its KG access
    at its next call rather than at the end of its TTL."""
    rows = connection.execute(text(
        "UPDATE external_agent_kg_tokens SET revoked_at = now() "
        "WHERE project_id = :p AND external_agent_id = :a AND revoked_at IS NULL "
        "RETURNING id, run_id, step_id"
    ), {"p": project_id, "a": external_agent_id}).fetchall()
    for row in rows:
        _audit(connection, project_id, "external_agent.token_revoked", token_id=row[0],
               external_agent_id=external_agent_id, run_id=row[1], step_id=row[2])
    return len(rows)


def revoke(connection: Connection, token_id: str) -> None:
    """Idempotent: ending an already-ended step must not raise. Only revoking a still-live token
    writes an audit event — ending an already-ended step must not duplicate it."""
    row = connection.execute(text(
        "UPDATE external_agent_kg_tokens SET revoked_at = now() "
        "WHERE id = :id AND revoked_at IS NULL "
        "RETURNING project_id, external_agent_id, run_id, step_id"
    ), {"id": token_id}).first()
    if row is not None:
        _audit(connection, row[0], "external_agent.token_revoked", token_id=token_id,
               external_agent_id=row[1], run_id=row[2], step_id=row[3])
