"""The security audit log as the console reads it (S4's `AuditEvent` / `AuditPage`,
apps/web/lib/identity/types.ts). Owned by stream S5. Read-only.

Filters (GET /v1/identity/audit): `q` (free text over the event type, what it touched, the actor's
name and id, the grant, the token and the request id), `outcome` (comma list, the console's
ok|denied|failed|notice or the stored success|denied|failure|info), `actor` (the acting principal
OR the person an agent acted for), `grant`, plus `kind`, `event` (`auth.*` prefix), `since`,
`until`, `cursor` (the last seq seen, as a string) and `limit`.

Every value shown was redacted when it was written (audit.prepare); `raw` is that stored row.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional

from . import audit

#: stored outcome -> the console's word, and back
OUTCOME_OUT = {"success": "ok", "failure": "failed", "denied": "denied", "info": "notice"}
OUTCOME_IN = {**{v: k for k, v in OUTCOME_OUT.items()}, **{k: k for k in OUTCOME_OUT}}

HEADLINES = {
    "auth.login.succeeded": "signed in", "auth.login.failed": "A sign-in failed",
    "auth.step_up": "confirmed it was them", "auth.logout": "signed out",
    "bootstrap.claimed": "claimed this BOSS", "owner.recovered_by_host":
    "An Owner was recovered from the host shell", "policy.changed": "changed the security policy",
    "user.invited": "invited people", "role_binding.added": "granted an access role",
    "role_binding.removed": "removed an access role", "api_key.created": "created a key",
    "api_key.revoked": "revoked a key", "delegation.created": "let agents act for them",
    "delegation.revoked": "revoked a grant", "delegation.extended": "extended a grant", "authz.denied": "was refused",
    "authz.shadow_deny": "would have been refused", "legacy.password_used":
    "used the shared password", "legacy.api_key_used": "used the shared API key",
    "legacy.network_used": "came in over the network bridge", "legacy.path_retired":
    "retired a legacy credential", "legacy.claimed_actor_ignored": "claimed to be someone else",
    "audit.verified": "verified the audit chain", "audit.exported": "exported the audit log",
    "audit.checkpoint": "pruned old audit events", "token.denied": "was refused a token",
    "egress.denied": "was refused through egress",
    "auth.mfa.succeeded": "confirmed with a second factor", "auth.mfa.failed": "entered a wrong second factor",
    "auth.passkey.succeeded": "signed in with a passkey", "auth.passkey.failed": "had a passkey refused",
    "auth.new_device": "signed in from a new device", "auth.locked_soft": "was locked out for a while",
    "auth.email_code.requested": "asked for an email code", "auth.email_code.succeeded": "signed in with an email code",
    "auth.email_code.failed": "entered a wrong email code", "auth.magic_link.requested": "asked for an email link",
    "auth.magic_link.used": "signed in with an email link", "auth.magic_link.rejected": "had an email link refused",
    "auth.signup.requested": "asked to sign up", "auth.signup.completed": "signed up",
    "auth.signup.refused": "was refused sign-up", "session.created": "started a session",
    "session.revoked": "ended a session", "token.issued": "was given a run token",
}


def _phrase(event_type: str) -> str:
    """An event with no headline of its own, in words: "auth.mfa.failed" never reads "auth mfa failed"."""
    words = event_type.split(".", 1)[1] if event_type.startswith(("auth.", "authz.")) else event_type
    return words.replace(".", " ").replace("_", " ")
_STANDALONE = {"auth.login.failed", "owner.recovered_by_host"}


def _sql(statement: str):
    from sqlalchemy import text
    return text(statement)


def hue(principal_id: Optional[str]) -> int:
    """The console's avatar colour (0-7), stable per principal."""
    if not principal_id:
        return 7
    return hashlib.sha256(principal_id.encode()).digest()[0] % 8


def public_id(seq: int) -> str:
    return f"sev_{seq}"


def _like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _names(db: Any, ids: Iterable[str]) -> dict[str, tuple[str, str]]:
    wanted = sorted({i for i in ids if i})
    if not wanted:
        return {}
    rows = db.execute(_sql("SELECT id, display_name, kind FROM identity_principals WHERE "
                           "id = ANY(:ids)"), {"ids": wanted}).all()
    return {r[0]: (r[1], r[2]) for r in rows}


def _split_hop(hop: str) -> tuple[str, Optional[str]]:
    agent, _, run = str(hop).partition("#")
    return agent, (run or None)


def to_console(row: Mapping[str, Any], names: Mapping[str, tuple[str, str]]) -> dict[str, Any]:
    """One stored row (audit.row_from_db) in the console's AuditEvent shape."""
    actor_id = row.get("actor_principal_id")
    actor_name, actor_kind = names.get(actor_id or "", (actor_id or "unknown", None))
    kind = row.get("actor_kind") or actor_kind or ("unknown" if not actor_id else "user")
    obo_id = row.get("on_behalf_of")
    details = dict(row.get("details") or {})
    chain_hops: list[dict[str, Any]] = []
    if obo_id:
        chain_hops.append({"id": obo_id, "name": names.get(obo_id, (obo_id, None))[0],
                           "run": None, "kind": "user"})
    run_id = details.get("run_id")
    for hop in row.get("chain") or []:
        agent, run = _split_hop(hop)
        chain_hops.append({"id": agent, "name": names.get(agent, (agent, None))[0],
                           "run": run[4:] if run and run.startswith("run_") else run,
                           "kind": "agent"})
        run_id = run or run_id
    if not row.get("chain"):
        chain_hops = []
    what = details.get("what") or " · ".join(str(x) for x in (
        row.get("permission"), row.get("scope"),
        None if row.get("permission") else row.get("target_id")) if x) or row["event_type"]
    phrase = HEADLINES.get(row["event_type"]) or _phrase(row["event_type"])
    headline = phrase if row["event_type"] in _STANDALONE or not actor_id \
        else f"{actor_name} {phrase}"
    ts = row.get("ts")
    raw = {k: row.get(k) for k in audit.HASHED_FIELDS}
    raw["ts"] = ts.isoformat() if isinstance(ts, datetime) else ts
    why = details.get("why") if isinstance(details.get("why"), list) else []
    return {
        "id": public_id(int(row["seq"])),
        "seq": int(row["seq"]),
        "at": raw["ts"],
        "event_type": row["event_type"],
        "actor": {"id": actor_id or "unknown", "kind": kind, "name": actor_name,
                  "hue": hue(actor_id)},
        "on_behalf_of": ({"id": obo_id, "name": names.get(obo_id, (obo_id, None))[0],
                          "hue": hue(obo_id)} if obo_id else None),
        "chain": chain_hops,
        "grant_id": row.get("grant_id"),
        "token_jti": row.get("token_jti"),
        "run_id": run_id,
        "request_id": row.get("request_id"),
        "what": str(what),
        "outcome": OUTCOME_OUT.get(row.get("outcome") or "", "notice"),
        "source": row.get("ip") or ("internal" if kind in ("agent", "system") else "console"),
        "headline": headline,
        "why": why,
        "raw": raw,
        "prev_hash": bytes(row["prev_hash"]).hex() if row.get("prev_hash") is not None else "",
        "hash": bytes(row["hash"]).hex() if row.get("hash") is not None else "",
    }


def _names_for(db: Any, rows: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    ids: list[str] = []
    for r in rows:
        ids += [r.get("actor_principal_id"), r.get("on_behalf_of")]
        ids += [_split_hop(h)[0] for h in (r.get("chain") or [])]
    return _names(db, ids)


def chain_status(org_id: str, *, db: Any) -> dict[str, Any]:
    """The banner over the log: when the chain was last verified, the head, and any break."""
    top = audit.head(org_id, db=db)
    last = audit.last_verified(org_id, db=db)
    details = (last or {}).get("details") or {}
    return {"verified_at": last["at"].isoformat() if last else None,
            "seq": top[0] if top else 0,
            "ok": bool(last["ok"]) if last else True,
            "first_break": details.get("first_break_seq") if last else None}


def page(org_id: str, *, db: Any, q: Optional[str] = None, outcome: Optional[str] = None,
         actor: Optional[str] = None, grant: Optional[str] = None, kind: Optional[str] = None,
         event: Optional[str] = None, since: Optional[datetime] = None,
         until: Optional[datetime] = None, cursor: Optional[str] = None,
         limit: int = 50) -> dict[str, Any]:
    """GET /v1/identity/audit: newest first, in S4's AuditPage shape."""
    where = ["e.org_id = :o"]
    n = max(1, min(int(limit), 500))
    params: dict[str, Any] = {"o": org_id, "n": n + 1}
    if q and q.strip():
        where.append("(e.event_type ILIKE :q OR coalesce(e.target_id, '') ILIKE :q OR "
                     "coalesce(e.target_type, '') ILIKE :q OR coalesce(e.permission, '') ILIKE :q "
                     "OR coalesce(e.actor_principal_id, '') ILIKE :q OR coalesce(e.on_behalf_of, "
                     "'') ILIKE :q OR coalesce(e.grant_id, '') ILIKE :q OR coalesce(e.token_jti, "
                     "'') ILIKE :q OR coalesce(e.request_id, '') ILIKE :q OR "
                     "coalesce(p.display_name, '') ILIKE :q OR "
                     "coalesce(e.details->>'what', '') ILIKE :q)")
        params["q"] = _like(q.strip()[:200])
    if outcome:
        wanted = sorted({OUTCOME_IN[o.strip()] for o in outcome.split(",")
                         if o.strip() in OUTCOME_IN})
        where.append("e.outcome = ANY(:outcomes)")
        params["outcomes"] = wanted or ["-none-"]
    if actor:
        where.append("(e.actor_principal_id = :actor OR e.on_behalf_of = :actor)")
        params["actor"] = actor
    if grant:
        where.append("e.grant_id = :grant")
        params["grant"] = grant
    if kind:
        where.append("e.actor_kind = :kind")
        params["kind"] = kind
    if event:
        if event.endswith("*"):
            where.append("e.event_type LIKE :ev")
            params["ev"] = _like(event[:-1])[1:]
        else:
            where.append("e.event_type = :ev")
            params["ev"] = event
    if since:
        where.append("e.ts >= :since")
        params["since"] = since
    if until:
        where.append("e.ts < :until")
        params["until"] = until
    if cursor:
        try:
            params["cursor"] = int(str(cursor).removeprefix("sev_"))
            where.append("e.seq < :cursor")
        except ValueError:
            pass
    columns = ", ".join(f"e.{c}" for c in ("id",) + audit.HASHED_FIELDS + ("prev_hash", "hash"))
    raw_rows = db.execute(_sql(
        f"SELECT {columns} FROM identity_security_events e LEFT JOIN identity_principals p ON "
        f"p.id = e.actor_principal_id WHERE {' AND '.join(where)} ORDER BY e.seq DESC LIMIT :n"),
        params).mappings().all()
    rows = [audit.row_from_db(r) for r in raw_rows[:n]]
    names = _names_for(db, rows)
    events = [to_console(r, names) for r in rows]
    more = len(raw_rows) > n
    return {"events": events, "cursor": str(events[-1]["seq"]) if more and events else None,
            "chain": chain_status(org_id, db=db)}


def one(org_id: str, event_ref: str, *, db: Any) -> Optional[dict[str, Any]]:
    """GET /v1/identity/audit/{id}: `sev_<seq>` (the console's id) or the numeric row id."""
    ref = str(event_ref)
    columns = ", ".join(("id",) + audit.HASHED_FIELDS + ("prev_hash", "hash"))
    if ref.startswith("sev_") and ref[4:].isdigit():
        clause, value = "seq = :v", int(ref[4:])
    elif ref.isdigit():
        clause, value = "id = :v", int(ref)
    else:
        return None
    raw = db.execute(_sql(f"SELECT {columns} FROM identity_security_events WHERE org_id = :o "
                          f"AND {clause}"), {"o": org_id, "v": value}).mappings().first()
    if raw is None:
        return None
    row = audit.row_from_db(raw)
    return to_console(row, _names_for(db, [row]))
