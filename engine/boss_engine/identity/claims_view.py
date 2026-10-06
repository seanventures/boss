"""The console's "Show token claims" view: what a run token under a grant SAID, never what it IS.
Owned by S3.

Built from the jti ledger (identity_run_credentials), not from any token: no signature, no raw
token, no DPoP key (`cnf.jkt` is shown redacted), no secret can appear. Since int-a the ledger
stores the token's redacted claims at mint time (`claims`, `scope`, `task`), so `claims` here is
exactly what the token carried (`scope_source: "token"`); `live_scope` is what it would be decided
against right now (person ∩ every ceiling ∩ grant ∩ task), shown next to it. A ledger row written
before those columns has no stored claims: its claims are rebuilt with the live intersection as
the scope (`scope_source: "live_intersection"`), labelled as such.
"""
from __future__ import annotations

from typing import Any, Optional

from .principal import ActorChain, AgentInstance, RunMode, TaskBinding


def _chain(raw: Any) -> ActorChain:
    return ActorChain(tuple(AgentInstance.parse(s) for s in (raw or [])))


def for_grant(grant: Any, *, run_id: Optional[str], db: Any) -> dict[str, Any]:
    from sqlalchemy import text

    from . import tokens

    row = db.execute(text(
        "SELECT jti, parent_jti, org_id, run_id, grant_id, subject_principal_id, mode, chain, depth, aud, scope_hash, "
        " issued_at, expires_at, revoked_at, task, scope, claims FROM identity_run_credentials WHERE grant_id = :g "
        " AND (CAST(:r AS TEXT) IS NULL OR run_id = :r) ORDER BY (revoked_at IS NULL AND expires_at > now()) DESC, "
        " issued_at DESC, depth DESC LIMIT 1"), {"g": grant.id, "r": run_id}).mappings().first()
    if row is None:
        return {"grant_id": grant.id, "live": False, "claims": None, "tokens": []}
    chain = _chain(row["chain"])
    stored_task = row["task"] if isinstance(row["task"], dict) else None
    task = TaskBinding(run_id=row["run_id"],
                       project_id=(stored_task or {}).get("project") or (grant.project_ids or (None,))[0],
                       step=(stored_task or {}).get("step"),
                       resources=dict((stored_task or {}).get("resources") or {}))
    try:
        state = tokens.live_state(db, org_id=row["org_id"], mode=RunMode(row["mode"]), sub=row["subject_principal_id"],
                                  chain=chain, grant_id=row["grant_id"], task=task)
        scope, live, why = sorted(state.allowed), row["revoked_at"] is None, None
    except tokens.TokenError as exc:
        scope, live, why = [], False, exc.reason
    stored = row["claims"] if isinstance(row["claims"], dict) else None
    if stored is not None:
        # What the token SAID at mint time. Redacted again here, whatever the row holds.
        claims = {k: v for k, v in stored.items() if k not in ("signature", "access_token", "token")}
        claims["cnf"] = {"jkt": tokens.REDACTED_CLAIM}
        token_scope = sorted(row["scope"] or str(stored.get("scope") or "").split())
        return {**_history(grant, row, db, live=live, why=why), "claims": claims, "scope_source": "token",
                "token_scope": token_scope, "live_scope": scope}
    claims: dict[str, Any] = {
        "iss": tokens.issuer(), "typ": tokens.TYP_RUN, "aud": row["aud"], "sub": row["subject_principal_id"],
        "boss_org": row["org_id"], "boss_mode": row["mode"], "boss_chain": chain.to_boss_chain(),
        "boss_depth": row["depth"], "boss_grant": row["grant_id"],
        "boss_task": {"run": row["run_id"], "project": task.project_id},
        "scope": " ".join(scope), "cnf": {"jkt": "[redacted]"}, "jti": row["jti"],
        "boss_parent_jti": row["parent_jti"], "iat": int(row["issued_at"].timestamp()),
        "exp": int(row["expires_at"].timestamp()),
    }
    act = chain.to_act_claim()
    if act is not None:
        claims["act"] = act
    return {**_history(grant, row, db, live=live, why=why), "scope_source": "live_intersection", "claims": claims,
            "token_scope": None, "live_scope": scope}


def _history(grant: Any, row: Any, db: Any, *, live: bool, why: Optional[str]) -> dict[str, Any]:
    from sqlalchemy import text

    history = db.execute(text(
        "SELECT jti, parent_jti, aud, depth, run_id, issued_at, expires_at, revoked_at FROM identity_run_credentials "
        "WHERE grant_id = :g ORDER BY issued_at DESC LIMIT 20"), {"g": grant.id}).mappings().all()
    return {
        "grant_id": grant.id, "run_id": row["run_id"], "live": live, "not_live_because": why,
        "scope_hash": row["scope_hash"],
        "tokens": [{"jti": h["jti"], "parent_jti": h["parent_jti"], "aud": h["aud"], "depth": h["depth"],
                    "run_id": h["run_id"], "issued_at": h["issued_at"].isoformat(),
                    "expires_at": h["expires_at"].isoformat(), "revoked": h["revoked_at"] is not None}
                   for h in history],
    }
