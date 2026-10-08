"""Lineage: every Passport, grant and token event, and the "who acted for whom" context. Owned by S3.

IDENTITY.md §7.12. Lineage lives in the security event log with `stream = lineage` (S5's
`audit.record`, written in the CALLER's transaction: if it cannot be written, the change fails).
Every token row carries jti and parent_jti, so any action walks back:
    action -> jti -> parent jti -> ... -> grant -> person
and forward from any grant to everything done under it.

This module also holds the ambient acting context for code that runs on behalf of a principal
outside an HTTP request (jobs, workers, workflow steps): `acting(...)` sets it, `current()` reads it.
Jobs stamp it at enqueue (R8) so the worker never authorizes an effect with its own identity.
"""
from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Optional

from .principal import AuthContext, RunTokenClaims


def scope_hash(scope: frozenset[str]) -> str:
    return hashlib.sha256(" ".join(sorted(scope)).encode("utf-8")).hexdigest()[:32]


def _record(db: Any, event: Any) -> None:
    from . import audit

    audit.record(event, db=db)


def passport_event(db: Any, passport: Any, event_type: str, *, ctx: Optional[AuthContext] = None,
                   actor_id: Optional[str] = None, details: Optional[Mapping[str, Any]] = None) -> None:
    from . import audit

    fields = dict(stream=audit.Stream.LINEAGE, target_type="agent", target_id=passport.agent_id,
                  details=dict(details or {}))
    if ctx is not None:
        event = audit.SecurityEvent.from_context(ctx, event_type, audit.Outcome.SUCCESS, **fields)
    else:
        event = audit.SecurityEvent(org_id=passport.org_id, event_type=event_type,
                                    outcome=audit.Outcome.SUCCESS, actor_principal_id=actor_id, **fields)
    _record(db, event)


def grant_event(db: Any, grant: Any, event_type: str, *, ctx: Optional[AuthContext] = None,
                details: Optional[Mapping[str, Any]] = None, outcome: Optional[str] = None) -> None:
    from . import audit

    fields = dict(stream=audit.Stream.LINEAGE, target_type="delegation_grant", target_id=grant.id,
                  details=dict(details or {}))
    out = audit.Outcome(outcome) if outcome else audit.Outcome.SUCCESS
    if ctx is not None:
        event = audit.SecurityEvent.from_context(ctx, event_type, out, **fields)
        if event.grant_id is None:
            event = dataclasses.replace(event, grant_id=grant.id)
    else:
        event = audit.SecurityEvent(org_id=grant.org_id, event_type=event_type, outcome=out,
                                    on_behalf_of=grant.user_principal_id, grant_id=grant.id, **fields)
    _record(db, event)


def token_event(db: Any, claims: RunTokenClaims, event_type: str, *, renewed_from: Optional[str] = None) -> None:
    """`token.issued` / `token.exchanged`: jti, parent_jti, sub, chain, grant, run, aud, scope hash —
    and, for a renewal, the jti it renewed (`renewed_from`), so the walk back never breaks."""
    from . import audit

    extra = {"renewed_from": renewed_from} if renewed_from else {}
    current = claims.chain.current
    _record(db, audit.SecurityEvent(
        org_id=claims.org_id, event_type=event_type, outcome=audit.Outcome.SUCCESS,
        stream=audit.Stream.LINEAGE, actor_principal_id=current.agent_id if current else claims.sub,
        actor_kind="agent", on_behalf_of=claims.sub if claims.mode.value == "obo" else None,
        grant_id=claims.grant_id, chain=claims.chain.to_boss_chain() or None, token_jti=claims.jti,
        target_type="run", target_id=claims.task.run_id,
        details={"parent_jti": claims.parent_jti, "aud": claims.aud, "depth": claims.depth,
                 "scope_hash": scope_hash(claims.scope), "mode": claims.mode.value,
                 "project": claims.task.project_id, "step": claims.task.step, **extra}))


# ---- reading lineage back (§7.12 "by agent") -----------------------------------------------------------

_LABELS = {
    "agent.passport.born": "Passport issued", "agent.passport.sealed": "Definition sealed",
    "agent.passport.ceiling_changed": "Ceiling changed", "agent.passport.sponsor_changed": "Sponsor changed",
    "agent.passport.suspended": "Suspended", "agent.passport.resumed": "Resumed",
    "agent.passport.revoked": "Revoked", "agent.passport.reissued": "Re-issued",
    "agent.passport.rules_changed": "Spawn rules changed", "agent.passport.flagged": "Flagged for review",
    "delegation.created": "Allowed to act for someone", "delegation.used_first": "First used a delegation",
    "delegation.narrowed": "Delegation narrowed", "delegation.revoked": "Delegation revoked",
    "delegation.expired": "Delegation expired", "delegation.suspended": "Delegation paused",
    "delegation.extended": "Delegation extended", "token.issued": "Started a run",
    "token.exchanged": "Handed work to a sub-agent", "token.denied": "Refused a token",
    "credential.handed_out": "Used a credential", "egress.denied": "Outbound call refused",
}


def _detail(event_type: str, details: Mapping[str, Any], target_id: Optional[str]) -> str:
    if event_type == "agent.passport.ceiling_changed":
        parts = [f"added {', '.join(details.get('added') or [])}" if details.get("added") else "",
                 f"removed {', '.join(details.get('removed') or [])}" if details.get("removed") else ""]
        return "; ".join(p for p in parts if p) or "no change"
    if event_type.startswith("token."):
        # The label already says "Started a run"; the run's id is not words (the console links it).
        return f"in {details['project']}" if details.get("project") else ""
    if event_type == "agent.passport.reissued":
        return f"from {details.get('from')}"
    if event_type == "delegation.created":
        return ", ".join(details.get("permissions") or [])[:200]
    reason = details.get("reason")
    return str(reason)[:200] if reason else ""


def for_agent(db: Any, org_id: str, agent_id: str, *, limit: int = 50,
              before: Optional[int] = None) -> dict[str, Any]:
    """An agent's lineage, newest first: lineage events where it is the target, the actor, in the
    chain, or named by the grant the event is under. `before` pages by event id."""
    from sqlalchemy import text

    limit = max(1, min(int(limit), 200))
    rows = db.execute(text(
        "SELECT e.id, e.ts, e.event_type, e.outcome, e.actor_principal_id, e.on_behalf_of, e.grant_id, "
        "e.token_jti, e.target_type, e.target_id, e.details FROM identity_security_events e "
        "WHERE e.org_id = :o AND e.stream = 'lineage' AND (CAST(:b AS BIGINT) IS NULL OR e.id < :b) AND ("
        " e.target_id = :a OR e.actor_principal_id = :a"
        " OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(CASE WHEN jsonb_typeof(e.chain) = 'array' "
        "            THEN e.chain ELSE '[]'::jsonb END) h WHERE split_part(h, '#', 1) = :a)"
        " OR e.grant_id IN (SELECT g.id FROM identity_delegation_grants g WHERE g.org_id = :o "
        "                   AND :a = ANY(g.agent_ids))) "
        "ORDER BY e.id DESC LIMIT :n"),
        {"o": org_id, "a": agent_id, "b": before, "n": limit + 1}).mappings().all()
    more = len(rows) > limit
    rows = rows[:limit]
    ids = {r["actor_principal_id"] for r in rows if r["actor_principal_id"]}
    names = {r[0]: r[1] for r in db.execute(text(
        "SELECT id, display_name FROM identity_principals WHERE id = ANY(:i)"), {"i": sorted(ids)}).all()} \
        if ids else {}
    events = []
    for r in rows:
        details = r["details"] if isinstance(r["details"], Mapping) else {}
        events.append({
            "id": int(r["id"]), "at": r["ts"].isoformat(), "event": r["event_type"],
            "label": _LABELS.get(r["event_type"], r["event_type"]),
            "detail": _detail(r["event_type"], details, r["target_id"]), "outcome": r["outcome"],
            "actor": ({"id": r["actor_principal_id"], "name": names.get(r["actor_principal_id"])}
                      if r["actor_principal_id"] else None),
            "on_behalf_of": r["on_behalf_of"], "grant_id": r["grant_id"],
            "run_id": r["target_id"] if r["target_type"] == "run" else details.get("run"),
            "jti": r["token_jti"], "parent_jti": details.get("parent_jti")})
    return {"agent_id": agent_id, "events": events,
            "next_before": events[-1]["id"] if more and events else None}


def denied(db: Any, *, org_id: str, event_type: str, reason: str, run_id: Optional[str] = None,
           grant_id: Optional[str] = None, chain: Optional[list[str]] = None, sub: Optional[str] = None,
           jti: Optional[str] = None, permission: Optional[str] = None,
           details: Optional[Mapping[str, Any]] = None) -> None:
    """A refused hop or a refused effect (`token.denied`, `authz.denied`, `egress.denied`).
    Best effort in its OWN transaction when possible: the refusal must be recorded even though the
    caller is about to roll back."""
    from . import audit

    event = audit.SecurityEvent(
        org_id=org_id or "", event_type=event_type, outcome=audit.Outcome.DENIED,
        stream=audit.Stream.LINEAGE if event_type.startswith("token.") else audit.Stream.SECURITY,
        actor_principal_id=(chain[-1].split("#")[0] if chain else sub), on_behalf_of=sub,
        grant_id=grant_id, chain=chain or None, token_jti=jti, target_type="run" if run_id else None,
        target_id=run_id, permission=permission, details={"reason": reason, **dict(details or {})})
    engine = getattr(db, "engine", None)
    try:
        if engine is not None:
            with engine.begin() as side:
                audit.record(event, db=side)
        else:
            audit.record(event, db=db)
    except Exception:  # noqa: BLE001 — a denial is still a denial
        import logging

        logging.getLogger(__name__).warning("could not record %s (%s)", event_type, reason)


# ---- the acting context (jobs, workers, steps) -------------------------------------------------------

@dataclass(frozen=True)
class Acting:
    """Who an effect is for, carried by every job (R8): never the worker's own identity."""
    principal_id: str                         # usr_ (obo) or agt_ (autonomous) or sys_
    org_id: str
    grant_id: Optional[str] = None
    chain: tuple[str, ...] = ()               # boss_chain
    run_id: Optional[str] = None
    jti: Optional[str] = None
    extra: Mapping[str, Any] = field(default_factory=dict)
    #: BOSS's own background work (no request, no person): stamped as `sys_worker`, marked system.
    system: bool = False

    def to_json(self) -> dict[str, Any]:
        out = {"on_behalf_of": self.principal_id, "org_id": self.org_id, "grant_id": self.grant_id,
               "chain": list(self.chain), "run_id": self.run_id, "jti": self.jti}
        if self.system:
            out["system"] = True
        return out

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Acting":
        return cls(principal_id=str(data["on_behalf_of"]), org_id=str(data.get("org_id") or ""),
                   grant_id=data.get("grant_id"), chain=tuple(data.get("chain") or ()),
                   run_id=data.get("run_id"), jti=data.get("jti"), system=bool(data.get("system")))

    @classmethod
    def from_context(cls, ctx: AuthContext) -> "Acting":
        rt = ctx.run_token
        if rt is not None:
            return cls(principal_id=rt.sub, org_id=rt.org_id, grant_id=rt.grant_id,
                       chain=tuple(rt.chain.to_boss_chain()), run_id=rt.task.run_id, jti=rt.jti)
        return cls(principal_id=ctx.principal.id, org_id=ctx.principal.org_id)


_ACTING: contextvars.ContextVar[Optional[Acting]] = contextvars.ContextVar("boss_identity_acting", default=None)
_CONTEXT: contextvars.ContextVar[Optional[AuthContext]] = contextvars.ContextVar("boss_identity_ctx", default=None)


nullcontext = contextlib.nullcontext


def current() -> Optional[Acting]:
    return _ACTING.get()


def current_context() -> Optional[AuthContext]:
    """The verified AuthContext this code runs under (a request's, or a delegated run's)."""
    return _CONTEXT.get()


@contextlib.contextmanager
def acting(who: Acting | AuthContext, *, tool_auth: Optional[AuthContext] = None) -> Iterator[Acting]:
    """Act for `who` in this block: jobs it enqueues carry it (R8), and — when `who` is a verified
    AuthContext (a request's, or a delegated run's run-token context) — every tool call in the
    block is decided as `who` (the registry's CURRENT_TOOL_AUTH), never as whoever called in
    (e.g. the worker). `tool_auth` names that context explicitly when `who` is an Acting."""
    value = who if isinstance(who, Acting) else Acting.from_context(who)
    token = _ACTING.set(value)
    ctx_token = _CONTEXT.set(who if isinstance(who, AuthContext) else _CONTEXT.get())
    decider = who if isinstance(who, AuthContext) else tool_auth
    tool_token = _set_tool_auth(decider) if decider is not None else None
    try:
        yield value
    finally:
        if tool_token is not None:
            _reset_tool_auth(tool_token)
        _CONTEXT.reset(ctx_token)
        _ACTING.reset(token)


def _set_tool_auth(ctx: AuthContext) -> Any:
    try:
        from ..tools.registry import CURRENT_TOOL_AUTH
    except Exception:  # noqa: BLE001 — the tool layer is optional for identity-only callers
        return None
    return (CURRENT_TOOL_AUTH, CURRENT_TOOL_AUTH.set(ctx))


def _reset_tool_auth(token: Any) -> None:
    var, tok = token
    var.reset(tok)


_ENFORCED: contextvars.ContextVar[Optional[bool]] = contextvars.ContextVar("boss_identity_enforced", default=None)


def mark_enforced(enforced: bool) -> None:
    """The API's guard: whether IT enforced this request (its per-route mode), for engine code that
    must decide the same way (approvals.deciding_context)."""
    _ENFORCED.set(bool(enforced))


def request_enforced() -> Optional[bool]:
    """True/False when a request's guard decided; None outside a request (tools, background)."""
    return _ENFORCED.get()


def enter(ctx: AuthContext) -> None:
    """Set the acting context for the rest of the current task WITHOUT a reset: the API's guard
    calls this once per request, and a request is its own task (its context dies with it). Code
    with a bounded scope uses `acting(...)` instead."""
    _ACTING.set(Acting.from_context(ctx))
    _CONTEXT.set(ctx)


# ---- jobs carry a principal (R8) ------------------------------------------------------------------------

JOB_IDENTITY_KEY = "_identity"


class PrincipalRequired(RuntimeError):
    """A leased job carries no principal at all (a row from before R8) while identity is
    enforced. Fail closed (R8)."""


def _enforced() -> bool:
    """One mode source: the same pydantic settings the API's guard reads (BOSS_AUTHZ_MODE from
    the environment OR the .env file), never os.environ alone — the two used to disagree."""
    from ..config import get_settings

    return (get_settings().authz_mode or "enforce").strip().lower() == "enforce"


_warned_unattributed: set[str] = set()

#: The principal BOSS's own background work is stamped with.
SYSTEM_PRINCIPAL = "sys_worker"
SYSTEM_ACTING = Acting(principal_id=SYSTEM_PRINCIPAL, org_id="", system=True)


#: Job kinds that ACT ON SOMEONE'S BEHALF (a build, a PR, an agent turn, a skill): one with no
#: principal fails closed at enqueue under enforcement, and is never quietly stamped sys_worker
#: (#8). Other kinds (sweeps, deliveries) are BOSS's own work.
DELEGATED_JOB_KINDS = frozenset({"code.task", "software_factory.propose_pr", "github.pull_request.merge",
                                 "github.repository.reconcile", "agent.resume", "agent.studio.execute",
                                 "agent.orchestrator.next_move", "skill.execute"})


def stamp_job(payload: dict[str, Any], who: Optional[Acting] = None, *, kind: Optional[str] = None) -> dict[str, Any]:
    """Stamp `payload["_identity"]` from `who` or the ambient context. A payload may not bring its
    own `_identity` (that would be an asserted identity): it is always overwritten.

    Never unattributed: work enqueued OUTSIDE any authenticated request or run (system threads —
    autopilot, the scheduler, captains — and public webhooks) is stamped as BOSS's own:
    `sys_worker`, marked `system`, logged once per call site. An authenticated HTTP request always
    carries the guard's acting context (identity_guard calls `enter(ctx)`), and a delegated run
    its own, so neither ever falls back to this. A system job authorizes nothing on anyone's
    behalf: it holds no grant, and the worker's identity never authorizes an effect (§7.14).
    The job ROW's identity columns come from `stamp_job_row` (which calls this): every INSERT INTO
    jobs goes through that, so the columns and the payload never disagree."""
    payload.pop(JOB_IDENTITY_KEY, None)
    acting_now = who if who is not None else current()
    if acting_now is None and kind in DELEGATED_JOB_KINDS:
        import logging

        if _enforced():
            raise PrincipalRequired(f"a {kind} job acts on someone's behalf and nobody is acting here")
        logging.getLogger(__name__).warning(
            "[identity] SHADOW: a %s job was enqueued with no principal; under BOSS_AUTHZ_MODE=enforce it "
            "is refused. It is stamped as system work and will hold no grant.", kind)
    if acting_now is None:
        import inspect
        import logging

        caller = next((f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno}" for f in inspect.stack()[2:5]
                       if not f.filename.endswith("identity/lineage.py")), "?")
        if caller not in _warned_unattributed:
            _warned_unattributed.add(caller)
            logging.getLogger(__name__).info("[identity] job enqueued outside any request at %s: stamped as "
                                             "system (%s)", caller, SYSTEM_PRINCIPAL)
        acting_now = SYSTEM_ACTING
    payload[JOB_IDENTITY_KEY] = acting_now.to_json()
    return payload


JOB_IDENTITY_COLUMNS = ("on_behalf_of", "grant_id", "chain", "on_behalf_system")


def stamp_job_row(payload: Optional[Mapping[str, Any]], who: Optional[Acting] = None, *,
                  kind: Optional[str] = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """(payload, columns) for one `INSERT INTO jobs`: the payload stamped by `stamp_job`, and the
    values for the row's `on_behalf_of`, `grant_id`, `chain` (JSON text, bind it with
    `CAST(:chain AS JSONB)`) and `on_behalf_system`. Every job enqueue path uses this; a raw
    INSERT that skipped it would be a job with no principal (R8)."""
    import json

    stamped = stamp_job(dict(payload or {}), who, kind=kind)
    data = stamped.get(JOB_IDENTITY_KEY) or {}
    chain = list(data.get("chain") or [])
    return stamped, {"on_behalf_of": data.get("on_behalf_of"), "grant_id": data.get("grant_id"),
                     "chain": json.dumps(chain) if data else None,
                     "on_behalf_system": bool(data.get("system"))}


def _acting_from_columns(job: Mapping[str, Any], extra: Mapping[str, Any]) -> Acting:
    import json

    chain = job.get("chain")
    if isinstance(chain, str):
        chain = json.loads(chain)
    same = bool(extra) and extra.get("on_behalf_of") == job["on_behalf_of"]
    # The columns are the authority. The payload copy only adds what has no column (org, run,
    # jti) and only when it names the same principal and grant.
    same = same and (extra.get("grant_id") or None) == (job.get("grant_id") or None)
    return Acting(principal_id=str(job["on_behalf_of"]),
                  org_id=str((extra.get("org_id") if same else None) or job.get("org_id") or ""),
                  grant_id=job.get("grant_id") or None, chain=tuple(chain or ()),
                  run_id=extra.get("run_id") if same else None, jti=extra.get("jti") if same else None,
                  system=bool(job.get("on_behalf_system")))


def acting_for_job(job: Mapping[str, Any]) -> Optional[Acting]:
    """The principal a leased job acts for. Reads the row's identity columns; `payload._identity`
    only for a row written before the columns existed (its `on_behalf_of` column is NULL)."""
    import json

    payload = job.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    data = payload.get(JOB_IDENTITY_KEY) if isinstance(payload, Mapping) else None
    if job.get("on_behalf_of"):
        return _acting_from_columns(job, data if isinstance(data, Mapping) else {})
    if not data:
        if _enforced():
            raise PrincipalRequired("this job carries no principal")
        return None
    return Acting.from_json(data)


def acting_for_job_id(db: Any, job_id: Optional[str]) -> Optional[Acting]:
    """`acting_for_job` for a job by id (the run path: a build or step started from a leased job)."""
    from sqlalchemy import text

    if not job_id:
        return None
    row = db.execute(text("SELECT payload, on_behalf_of, grant_id, chain, on_behalf_system FROM jobs WHERE id = :j"),
                     {"j": job_id}).mappings().first()
    if row is None:
        return None
    return acting_for_job(dict(row))
