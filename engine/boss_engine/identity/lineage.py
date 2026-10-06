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


def token_event(db: Any, claims: RunTokenClaims, event_type: str) -> None:
    """`token.issued` / `token.exchanged`: jti, parent_jti, sub, chain, grant, run, aud, scope hash."""
    from . import audit

    current = claims.chain.current
    _record(db, audit.SecurityEvent(
        org_id=claims.org_id, event_type=event_type, outcome=audit.Outcome.SUCCESS,
        stream=audit.Stream.LINEAGE, actor_principal_id=current.agent_id if current else claims.sub,
        actor_kind="agent", on_behalf_of=claims.sub if claims.mode.value == "obo" else None,
        grant_id=claims.grant_id, chain=claims.chain.to_boss_chain() or None, token_jti=claims.jti,
        target_type="run", target_id=claims.task.run_id,
        details={"parent_jti": claims.parent_jti, "aud": claims.aud, "depth": claims.depth,
                 "scope_hash": scope_hash(claims.scope), "mode": claims.mode.value,
                 "project": claims.task.project_id, "step": claims.task.step}))


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

    def to_json(self) -> dict[str, Any]:
        return {"on_behalf_of": self.principal_id, "org_id": self.org_id, "grant_id": self.grant_id,
                "chain": list(self.chain), "run_id": self.run_id, "jti": self.jti}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Acting":
        return cls(principal_id=str(data["on_behalf_of"]), org_id=str(data.get("org_id") or ""),
                   grant_id=data.get("grant_id"), chain=tuple(data.get("chain") or ()),
                   run_id=data.get("run_id"), jti=data.get("jti"))

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


def enter(ctx: AuthContext) -> None:
    """Set the acting context for the rest of the current task WITHOUT a reset: the API's guard
    calls this once per request, and a request is its own task (its context dies with it). Code
    with a bounded scope uses `acting(...)` instead."""
    _ACTING.set(Acting.from_context(ctx))
    _CONTEXT.set(ctx)


# ---- jobs carry a principal (R8) ------------------------------------------------------------------------

JOB_IDENTITY_KEY = "_identity"


class PrincipalRequired(RuntimeError):
    """A job was enqueued with no principal while identity is enforced. Fail closed (R8)."""


def _enforced() -> bool:
    import os

    return (os.environ.get("BOSS_AUTHZ_MODE") or "shadow").strip().lower() == "enforce"


_warned_unattributed: set[str] = set()


def stamp_job(payload: dict[str, Any], who: Optional[Acting] = None) -> dict[str, Any]:
    """Stamp `payload["_identity"]` from `who` or the ambient context. A payload may not bring its
    own `_identity` (that would be an asserted identity): it is always overwritten or refused.
    The job ROW's identity columns come from `stamp_job_row` (which calls this): every INSERT INTO
    jobs goes through that, so the columns and the payload never disagree."""
    payload.pop(JOB_IDENTITY_KEY, None)
    acting_now = who if who is not None else current()
    if acting_now is None:
        if _enforced():
            raise PrincipalRequired("a job must carry the principal it acts for")
        import inspect
        import logging

        caller = next((f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno}" for f in inspect.stack()[2:4]), "?")
        if caller not in _warned_unattributed:
            _warned_unattributed.add(caller)
            logging.getLogger(__name__).info("[identity] job enqueued without a principal at %s (shadow)", caller)
        return payload
    payload[JOB_IDENTITY_KEY] = acting_now.to_json()
    return payload


JOB_IDENTITY_COLUMNS = ("on_behalf_of", "grant_id", "chain")


def stamp_job_row(payload: Optional[Mapping[str, Any]], who: Optional[Acting] = None
                  ) -> tuple[dict[str, Any], dict[str, Any]]:
    """(payload, columns) for one `INSERT INTO jobs`: the payload stamped by `stamp_job`, and the
    values for the row's `on_behalf_of`, `grant_id` and `chain` (JSON text, bind it with
    `CAST(:chain AS JSONB)`). Every job enqueue path uses this; a raw INSERT that skipped it
    would be a job with no principal (R8). Fails closed under enforce, like `stamp_job`."""
    import json

    stamped = stamp_job(dict(payload or {}), who)
    data = stamped.get(JOB_IDENTITY_KEY) or {}
    chain = list(data.get("chain") or [])
    return stamped, {"on_behalf_of": data.get("on_behalf_of"), "grant_id": data.get("grant_id"),
                     "chain": json.dumps(chain) if data else None}


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
                  run_id=extra.get("run_id") if same else None, jti=extra.get("jti") if same else None)


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
    row = db.execute(text("SELECT payload, on_behalf_of, grant_id, chain FROM jobs WHERE id = :j"),
                     {"j": job_id}).mappings().first()
    if row is None:
        return None
    return acting_for_job(dict(row))
