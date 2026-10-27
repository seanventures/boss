"""Console deep links resolve an object to its OWN project before anything renders (Build L).

`resolve_owner` is the one shared "who owns this id" lookup every console object type uses: it
reuses the loaders `boss_engine.identity.object_kinds` already registers for `Scope.of(...)`, so
no per-type SQL is duplicated here. Not-found and access-denied are answered with the identical
shape (N5, anti-enumeration) — the caller never learns which one happened.
"""
from __future__ import annotations

import collections
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import text

from ..identity import authz, object_kinds
from ..identity.permissions_gen import P
from ..identity.principal import AuthContext
from ..identity.scope import ResolvedScope

log = logging.getLogger(__name__)

#: `console_resolve_owner_total{object_type,result}` (spec §7 N6 / acceptance #2). Kept as an
#: in-process counter (read by the Observability scorecards exporter and by tests) rather than a
#: new scrape surface — this process already reports its own request metrics elsewhere.
RESOLVE_OWNER_METRIC: "collections.Counter[tuple[str, str]]" = collections.Counter()


def resolve_owner_metric_value(object_type: str, result: str) -> int:
    return RESOLVE_OWNER_METRIC[(object_type, result)]

#: console-facing object_type (as named in a URL param) -> the kind registered in object_kinds.
CONSOLE_OBJECT_KINDS: dict[str, str] = {
    "build": "code_task",
    "meeting": "meeting",
    "workflow_run": "workflow_run",
    "decision": "decision",
    "self_fix": "self_fix",
    "shepherd_escalation": "shepherd_escalation",
    "agent_thread": "agent_thread",
}


@dataclass(frozen=True)
class ResolveOwnerResult:
    project_id: Optional[str]
    project_name: Optional[str]
    can_view: bool

    @classmethod
    def denied(cls) -> "ResolveOwnerResult":
        return cls(None, None, False)


def _denied(ctx: AuthContext, object_type: str, object_id: str) -> ResolveOwnerResult:
    _record(ctx, object_type, object_id, "denied")
    return ResolveOwnerResult.denied()


def _record(ctx: AuthContext, object_type: str, object_id: str, result: str) -> None:
    """N2: one line per resolve, naming who asked, what for, and the outcome — never the
    requester's other project memberships, so this never becomes an enumeration log itself."""
    RESOLVE_OWNER_METRIC[(object_type, result)] += 1
    log.info("console_resolve_owner actor=%s requested_id=%s result=%s ts=%s",
              getattr(ctx.principal, "id", None), object_id, result, time.time())


def resolve_owner(db: Any, ctx: AuthContext, object_type: str, object_id: str) -> ResolveOwnerResult:
    """(project_id, project_name, can_view) for a console deep link.

    Fails closed: an unknown `object_type`, a nonexistent object, an object in another org, and
    an object the caller may not view all answer the same `denied()` shape (N1, N5) — the
    distinction lives only in the caller's own audit log, never in this response.
    """
    kind = CONSOLE_OBJECT_KINDS.get(object_type)
    if kind is None:
        return _denied(ctx, object_type, object_id)
    try:
        spec = object_kinds.get(kind)
    except KeyError:
        return _denied(ctx, object_type, object_id)
    try:
        loaded = spec.load(db, object_id)
    except Exception:
        return _denied(ctx, object_type, object_id)
    if loaded is None:
        return _denied(ctx, object_type, object_id)
    org_id, project_id = loaded
    if not project_id or org_id != ctx.principal.org_id:
        return _denied(ctx, object_type, object_id)
    decision = authz.check(ctx, P.PROJECT_READ, ResolvedScope.in_project(org_id, project_id), db=db)
    if not decision.allowed:
        return _denied(ctx, object_type, object_id)
    name = db.execute(text("SELECT name FROM projects WHERE id = :p"), {"p": project_id}).scalar()
    _record(ctx, object_type, object_id, "resolved")
    return ResolveOwnerResult(project_id, name, True)
