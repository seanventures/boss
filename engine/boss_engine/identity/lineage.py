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


def current() -> Optional[Acting]:
    return _ACTING.get()


def current_context() -> Optional[AuthContext]:
    """The verified AuthContext this code runs under (a request's, or a delegated run's)."""
    return _CONTEXT.get()


@contextlib.contextmanager
def acting(who: Acting | AuthContext) -> Iterator[Acting]:
    value = who if isinstance(who, Acting) else Acting.from_context(who)
    token = _ACTING.set(value)
    ctx_token = _CONTEXT.set(who if isinstance(who, AuthContext) else _CONTEXT.get())
    try:
        yield value
    finally:
        _CONTEXT.reset(ctx_token)
        _ACTING.reset(token)


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
    own `_identity` (that would be an asserted identity): it is always overwritten or refused."""
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


def acting_for_job(job: Mapping[str, Any]) -> Optional[Acting]:
    """The principal a leased job acts for (the API wraps the job's execution in `acting(...)`)."""
    import json

    payload = job.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    data = payload.get(JOB_IDENTITY_KEY) if isinstance(payload, Mapping) else None
    if not data:
        if _enforced():
            raise PrincipalRequired("this job carries no principal")
        return None
    return Acting.from_json(data)
