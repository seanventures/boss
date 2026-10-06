"""Authorization: the one decision function (the PDP). Owned by stream S2.

FROZEN IN PHASE 0: Decision, Why, DenyReason, EffectivePermissions and the signatures. The bodies
marked NotImplementedError are S2's (IDENTITY_BUILD.md §3). IDENTITY.md §6.

`check` is used by API routes (through the enforcement point), MCP tools, server-rendered console
data, the token service and the egress proxy. Nothing else decides. The algorithm is IDENTITY.md
§6.6, in order: catalogue -> liveness of everything in the context -> system fixed sets -> base set
(person / OBO intersection / autonomous / key scopes) -> step-up -> decision.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Optional

from .principal import AuthContext, Principal
from .scope import ResolvedScope


class DenyReason(StrEnum):
    UNAUTHENTICATED = "unauthenticated"          # 401
    STEP_UP_REQUIRED = "step_up_required"        # 401 {"error": "step_up_required"}
    MISSING_PERMISSION = "missing_permission"    # 403 {"error": "forbidden", "permission": ...}
    NOT_DELEGABLE = "not_delegable"              # 403: an agent token, a non-delegable permission
    UNKNOWN_PERMISSION = "unknown_permission"    # 403 (and 500 in dev/test)
    PRINCIPAL_INACTIVE = "principal_inactive"    # 401
    CREDENTIAL_INVALID = "credential_invalid"    # 401: key revoked/expired/CIDR, jti revoked
    GRANT_INACTIVE = "grant_inactive"            # 403: grant revoked/expired/suspended
    CHAIN_INACTIVE = "chain_inactive"            # 403: an agent in the chain suspended/revoked
    OUT_OF_SCOPE = "out_of_scope"                # 403: key/grant/task project limits
    ORG_ONLY_AT_PROJECT = "org_only_at_project"  # 403: an org-only permission at a project scope
    NOT_FOUND = "not_found"                      # 404: invisible object (no existence oracle)
    UNDECLARED_ROUTE = "undeclared_route"        # 403: deny by default
    NOT_EVALUATED = "not_evaluated"              # shadow only: a stream's piece is not built yet


DENY_STATUS: dict[DenyReason, int] = {
    DenyReason.UNAUTHENTICATED: 401, DenyReason.STEP_UP_REQUIRED: 401,
    DenyReason.PRINCIPAL_INACTIVE: 401, DenyReason.CREDENTIAL_INVALID: 401,
    DenyReason.NOT_FOUND: 404, DenyReason.NOT_EVALUATED: 503,
}


@dataclass(frozen=True)
class Why:
    """One link of an allow's explanation: `role operator <- group Builders <- binding at
    project:acme-web`. A direct binding has no group."""
    role_key: str
    binding_id: str
    binding_scope: str                 # "org" | "project:<id>"
    via_group: Optional[str] = None    # group name
    group_source: Optional[str] = None  # manual | scim | oidc

    def __str__(self) -> str:
        parts = [f"role {self.role_key}"]
        if self.via_group:
            src = f" (from {self.group_source})" if self.group_source and self.group_source != "manual" else ""
            parts.append(f"group {self.via_group}{src}")
        parts.append(f"binding at {self.binding_scope}")
        return " ← ".join(parts)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    permission: str
    scope: Optional[ResolvedScope]
    reason: Optional[DenyReason] = None           # None when allowed
    via: tuple[Why, ...] = ()                     # allow: the why-chain(s)
    narrowed_by: tuple[str, ...] = ()             # deny after narrowing: "key_scopes", "ceiling:agt_forge", "grant:dlg_x", "task"

    @property
    def status(self) -> int:
        if self.allowed:
            return 200
        return DENY_STATUS.get(self.reason or DenyReason.MISSING_PERMISSION, 403)

    def error_body(self) -> dict[str, Any]:
        """The API error body (IDENTITY.md §10). Never names more than the missing permission."""
        if self.allowed:
            return {}
        reason = self.reason or DenyReason.MISSING_PERMISSION
        if reason in (DenyReason.UNAUTHENTICATED, DenyReason.PRINCIPAL_INACTIVE,
                      DenyReason.CREDENTIAL_INVALID):
            return {"error": "unauthenticated"}
        if reason == DenyReason.STEP_UP_REQUIRED:
            return {"error": "step_up_required"}
        if reason == DenyReason.NOT_FOUND:
            return {"error": "not_found"}
        if reason == DenyReason.NOT_EVALUATED:
            return {"error": "identity_unavailable"}
        return {"error": "forbidden", "permission": self.permission}

    @classmethod
    def allow(cls, permission: str, scope: Optional[ResolvedScope], via: tuple[Why, ...] = ()) -> "Decision":
        return cls(True, permission, scope, None, via)

    @classmethod
    def deny(cls, permission: str, scope: Optional[ResolvedScope], reason: DenyReason,
             narrowed_by: tuple[str, ...] = ()) -> "Decision":
        return cls(False, permission, scope, reason, (), narrowed_by)


@dataclass(frozen=True)
class EffectivePermissions:
    """What a principal (or a narrowed credential/run token) holds at one scope, right now.
    This is exactly what GET /v1/identity/me/permissions returns (contracts.MePermissions)."""
    permissions: frozenset[str]
    scope: ResolvedScope
    epoch: int
    step_up_fresh_until: Optional[datetime] = None
    why: dict[str, tuple[Why, ...]] = field(default_factory=dict)  # filled only when asked


# =================================================================================================
# The implementation (stream S2). Everything below reads the database through `db=` (a SQLAlchemy
# Connection in the caller's transaction) or, when None, a short connection of its own.
# =================================================================================================

import logging  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from collections import OrderedDict  # noqa: E402
from datetime import timedelta, timezone  # noqa: E402

from sqlalchemy import text  # noqa: E402

from . import epoch as _epoch  # noqa: E402
from . import object_kinds as _object_kinds  # noqa: E402,F401 — registers every Scope.of loader
from .permissions_gen import PERMISSION_META, ROLE_PERMISSIONS, SYSTEM_PRINCIPALS  # noqa: E402
from .principal import AuthMethod, PrincipalKind, RunMode  # noqa: E402
from .scope import ScopeKind  # noqa: E402

LOG = logging.getLogger("boss.identity.authz")

#: The fixed set of the legacy console key (legacy.LEGACY_CONSOLE_PRINCIPAL): Owner-equivalent until
#: the cutoff (IDENTITY.md §12, D25). Named here so the system sets are one table.
LEGACY_CONSOLE_PRINCIPAL = "sys_legacy_console"

#: Credentials that cannot carry a second factor at all: the migration bridges and in-process
#: system principals. Step-up is satisfied for them (they are time-boxed and shown in posture);
#: a PAT or service-account key never satisfies it (A26), a session only within the window.
STEP_UP_EXEMPT_METHODS = frozenset({AuthMethod.LEGACY_PASSWORD, AuthMethod.LEGACY_API_KEY,
                                    AuthMethod.NETWORK, AuthMethod.OIDC_BEARER, AuthMethod.SYSTEM})

DEFAULT_STEP_UP_SECONDS = 600


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---- system principals: fixed sets ---------------------------------------------------------------

def system_permissions(principal_id: str, *, db: Any = None) -> frozenset[str]:
    """The fixed set of a system principal (§6.6 step 2). Unknown system ids hold nothing.
    The legacy bridge principals (sys_legacy_*, sys_scim_legacy) take theirs from S5's
    `legacy.system_permissions`, which also empties them once their path is retired."""
    from . import legacy

    legacy_set = getattr(legacy, "system_permissions", None)
    if legacy_set is not None:
        fixed = legacy_set(principal_id, db=db)
        if fixed is not None:
            return frozenset(fixed)
    if principal_id == LEGACY_CONSOLE_PRINCIPAL:
        return ROLE_PERMISSIONS["owner"]
    meta = SYSTEM_PRINCIPALS.get(principal_id)
    if meta is None and principal_id.startswith("sys_scim_"):
        meta = SYSTEM_PRINCIPALS.get("sys_scim")
    if meta is None or meta.access_role == "none":
        return frozenset()
    return ROLE_PERMISSIONS.get(meta.access_role, frozenset())


# ---- what a principal holds through bindings ------------------------------------------------------

_HELD_SQL = text("""
SELECT b.id AS binding_id, b.scope_type, b.scope_id, r.key AS role_key, r.system AS role_system,
       g.name AS group_name, g.source AS group_source, rp.permission_key
  FROM identity_role_bindings b
  JOIN identity_access_roles r ON r.id = b.role_id AND (r.org_id IS NULL OR r.org_id = :org)
  JOIN identity_access_role_permissions rp ON rp.role_id = r.id
  LEFT JOIN identity_groups g ON b.subject_type = 'group' AND g.id = b.subject_id
 WHERE b.org_id = :org
   AND (b.expires_at IS NULL OR b.expires_at > now())
   AND (   (b.subject_type = 'principal' AND b.subject_id = :pid)
        OR (b.subject_type = 'group' AND g.org_id = :org AND b.subject_id IN (
              SELECT m.group_id FROM identity_group_members m WHERE m.principal_id = :pid)))
   AND (   (b.scope_type = 'org' AND b.scope_id = :org)
        OR (b.scope_type = 'project' AND b.scope_id = :project))
""")


@dataclass(frozen=True)
class _Held:
    permissions: frozenset[str]
    why: dict[str, tuple[Why, ...]]


class _HeldCache:
    """(org, principal, scope label, epoch) -> what the principal holds there. Bounded LRU; the
    epoch in the key makes every identity mutation a miss, so nothing here is ever stale."""

    def __init__(self, size: int = 8192) -> None:
        self._lock = threading.Lock()
        self._data: OrderedDict[tuple, _Held] = OrderedDict()
        self._size = size

    def get(self, key: tuple) -> Optional[_Held]:
        with self._lock:
            hit = self._data.get(key)
            if hit is not None:
                self._data.move_to_end(key)
            return hit

    def put(self, key: tuple, value: _Held) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self._size:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


HELD_CACHE = _HeldCache()


def _held(conn: Any, org_id: str, principal_id: str, scope: ResolvedScope, epoch: int) -> _Held:
    project = scope.project_id if scope.kind == ScopeKind.PROJECT else None
    key = (org_id, principal_id, project, epoch)
    hit = HELD_CACHE.get(key)
    if hit is not None:
        return hit
    why: dict[str, list[Why]] = {}
    rows = conn.execute(_HELD_SQL, {"org": org_id, "pid": principal_id,
                                    "project": project or ""}).mappings().all()
    for r in rows:
        meta = PERMISSION_META.get(r["permission_key"])
        if meta is None or meta.system_only or r["role_system"]:
            continue  # a person never holds a system permission through a binding
        if r["scope_type"] == "project" and meta.org_only:
            continue  # amendment A3: an org-only permission is inert in a project binding
        label = "org" if r["scope_type"] == "org" else f"project:{r['scope_id']}"
        why.setdefault(meta.key, []).append(Why(
            role_key=r["role_key"], binding_id=r["binding_id"], binding_scope=label,
            via_group=r["group_name"], group_source=r["group_source"]))
    held = _Held(frozenset(why), {k: tuple(v) for k, v in why.items()})
    HELD_CACHE.put(key, held)
    return held


# ---- liveness ------------------------------------------------------------------------------------

def _statuses(conn: Any, org_id: str, ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    rows = conn.execute(text(
        "SELECT id, status FROM identity_principals WHERE org_id = :o AND id = ANY(:ids)"),
        {"o": org_id, "ids": list(ids)}).all()
    return {str(r[0]): str(r[1]) for r in rows}


def _project_in_org(conn: Any, org_id: str, project_id: str) -> bool:
    return conn.execute(text(
        "SELECT 1 FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
        "WHERE p.id = :p AND w.org_id = :o"), {"p": project_id, "o": org_id}).first() is not None


def _step_up_window_seconds(org_id: str, conn: Any) -> int:
    try:
        from .policy import get_policy

        return int(get_policy(org_id, db=conn).step_up_minutes) * 60
    except NotImplementedError:
        return DEFAULT_STEP_UP_SECONDS
    except Exception:  # noqa: BLE001 — a broken policy row must not widen the window
        return DEFAULT_STEP_UP_SECONDS


def _step_up_exempt(ctx: AuthContext) -> bool:
    """In-process system principals, and the legacy bridges as S5 defines them
    (`legacy.step_up_exempt`: a bridged context with no session and no run token)."""
    if ctx.run_token is not None:
        return False
    if ctx.method == AuthMethod.SYSTEM:
        return True
    from . import legacy

    exempt = getattr(legacy, "step_up_exempt", None)
    if exempt is not None:
        return bool(exempt(ctx))
    return ctx.method in STEP_UP_EXEMPT_METHODS and ctx.session is None


def step_up_fresh_until(ctx: AuthContext, *, window_seconds: int = DEFAULT_STEP_UP_SECONDS
                        ) -> Optional[datetime]:
    """When the context's step-up stops being fresh; None when it is not fresh at all."""
    if _step_up_exempt(ctx):
        return datetime.max.replace(tzinfo=timezone.utc)
    if ctx.session is None or ctx.session.mfa_at is None or ctx.run_token is not None:
        return None
    mfa_at = ctx.session.mfa_at
    if mfa_at.tzinfo is None:
        mfa_at = mfa_at.replace(tzinfo=timezone.utc)
    return mfa_at + timedelta(seconds=window_seconds)


# ---- the base set (§6.6 step 3) ------------------------------------------------------------------

@dataclass
class _Base:
    permissions: frozenset[str]
    why: dict[str, tuple[Why, ...]]
    #: permission -> what removed it (only for permissions the person held before narrowing)
    removed_by: dict[str, tuple[str, ...]]
    deny: Optional[tuple[DenyReason, tuple[str, ...]]] = None   # a context-wide refusal


def _narrow(base: set[str], allowed: frozenset[str] | set[str], label: str,
            removed: dict[str, list[str]]) -> set[str]:
    for k in base - set(allowed):
        removed.setdefault(k, []).append(label)
    return base & set(allowed)


def _ceiling(conn: Any, agent_id: str) -> frozenset[str]:
    rows = conn.execute(text(
        "SELECT permission_key FROM identity_agent_ceilings WHERE agent_id = :a"),
        {"a": agent_id}).all()
    return frozenset(str(r[0]) for r in rows)


def _base(conn: Any, ctx: AuthContext, scope: ResolvedScope, epoch: int) -> _Base:
    org = ctx.principal.org_id
    rt = ctx.run_token
    removed: dict[str, list[str]] = {}
    if rt is None:
        held = _held(conn, org, ctx.principal.id, scope, epoch)
        perms = set(held.permissions)
        cred = ctx.credential
        if cred is not None:
            perms = _narrow(perms, cred.scopes, "key_scopes", removed)
        return _Base(frozenset(perms), held.why, {k: tuple(v) for k, v in removed.items()})

    # ---- an agent run: the run token's live intersection (§6.6, §7.4) ----
    if rt.org_id != org:
        return _Base(frozenset(), {}, {}, (DenyReason.CREDENTIAL_INVALID, ("org",)))
    row = conn.execute(text(
        "SELECT revoked_at, expires_at FROM identity_run_credentials WHERE jti = :j AND org_id = :o"),
        {"j": rt.jti, "o": org}).first()
    if row is None or row[0] is not None or (row[1] is not None and row[1] <= _now()):
        return _Base(frozenset(), {}, {}, (DenyReason.CREDENTIAL_INVALID, ("jti",)))
    agents = list(rt.chain.agent_ids)
    if rt.mode == RunMode.AUTONOMOUS and ctx.principal.id not in agents:
        agents.append(ctx.principal.id)
    statuses = _statuses(conn, org, agents)
    dead = [a for a in agents if statuses.get(a) != "active"]
    if dead:
        return _Base(frozenset(), {}, {}, (DenyReason.CHAIN_INACTIVE, tuple(dead)))
    task = rt.task
    if task.project_id and scope.project_id != task.project_id:
        return _Base(frozenset(), {}, {}, (DenyReason.OUT_OF_SCOPE, ("task",)))
    subject = rt.sub if rt.mode == RunMode.OBO else ctx.principal.id
    held = _held(conn, org, subject, scope, epoch)
    perms = set(held.permissions)
    for agent in agents:
        perms = _narrow(perms, _ceiling(conn, agent), f"ceiling:{agent}", removed)
    if rt.mode == RunMode.OBO:
        if not rt.grant_id:
            return _Base(frozenset(), {}, {}, (DenyReason.GRANT_INACTIVE, ("grant:none",)))
        grant = conn.execute(text(
            "SELECT status, expires_at, project_ids, agent_ids, user_principal_id "
            "FROM identity_delegation_grants WHERE id = :g AND org_id = :o"),
            {"g": rt.grant_id, "o": org}).mappings().first()
        if (grant is None or grant["status"] != "active" or grant["user_principal_id"] != rt.sub
                or (grant["expires_at"] is not None and grant["expires_at"] <= _now())):
            return _Base(frozenset(), {}, {}, (DenyReason.GRANT_INACTIVE, (f"grant:{rt.grant_id}",)))
        if grant["agent_ids"] and any(a not in grant["agent_ids"] for a in rt.chain.agent_ids):
            return _Base(frozenset(), {}, {}, (DenyReason.GRANT_INACTIVE, (f"grant:{rt.grant_id}",)))
        if grant["project_ids"] and scope.project_id not in grant["project_ids"]:
            return _Base(frozenset(), {}, {}, (DenyReason.OUT_OF_SCOPE, (f"grant:{rt.grant_id}",)))
        grant_perms = frozenset(str(r[0]) for r in conn.execute(text(
            "SELECT permission_key FROM identity_delegation_grant_permissions WHERE grant_id = :g"),
            {"g": rt.grant_id}).all())
        perms = _narrow(perms, grant_perms, f"grant:{rt.grant_id}", removed)
    perms = _narrow(perms, rt.scope, "token_scope", removed)
    return _Base(frozenset(perms), held.why, {k: tuple(v) for k, v in removed.items()})


# ---- audit (S5's record; best effort until it lands, never in the caller's transaction) -----------

_AUDIT_SEEN: dict[tuple, float] = {}
_AUDIT_LOCK = threading.Lock()
AUDIT_REPEAT_SECONDS = 60.0


def _audit(ctx: AuthContext, decision: Decision, event_type: str) -> None:
    key = (ctx.principal.id, decision.permission,
           decision.scope.label() if decision.scope else "", event_type,
           decision.reason.value if decision.reason else "")
    now = time.monotonic()
    with _AUDIT_LOCK:
        last = _AUDIT_SEEN.get(key)
        if last is not None and now - last < AUDIT_REPEAT_SECONDS:
            return   # identical denials are counted by the ledger, not chained one by one
        _AUDIT_SEEN[key] = now
        if len(_AUDIT_SEEN) > 20000:
            _AUDIT_SEEN.clear()
    try:
        from . import audit

        event = audit.SecurityEvent.from_context(
            ctx, event_type,
            audit.Outcome.DENIED if not decision.allowed else audit.Outcome.SUCCESS,
            permission=decision.permission,
            scope=decision.scope.label() if decision.scope else None,
            target_type=decision.scope.object_kind if decision.scope else None,
            target_id=decision.scope.object_id if decision.scope else None,
            details={"reason": decision.reason.value if decision.reason else None,
                     "narrowed_by": list(decision.narrowed_by),
                     "via": [str(w) for w in decision.via]})
        with _epoch.connection(None, write=True) as conn:
            audit.record(event, db=conn)
    except NotImplementedError:
        pass   # S5's audit.record has not landed
    except Exception as exc:  # noqa: BLE001 — an audit failure never changes the decision
        LOG.warning("identity: could not audit %s: %s", event_type, type(exc).__name__)


# ---- the decision --------------------------------------------------------------------------------

def _finish(ctx: AuthContext, decision: Decision) -> Decision:
    if not decision.allowed:
        _audit(ctx, decision, "authz.denied")
    elif PERMISSION_META[decision.permission].critical:
        _audit(ctx, decision, "authz.allowed_critical")
    return decision


def _invisible(permission: str, scope: ResolvedScope, held: frozenset[str]) -> bool:
    """An object the caller cannot even read is answered 404, never 403 (no existence oracle)."""
    if not scope.object_kind:
        return False
    read_key = permission.split(":", 1)[0] + ":read"
    return read_key not in held


def check(ctx: AuthContext, permission: str, scope: ResolvedScope, *, db: Any = None) -> Decision:
    """THE decision. Deny by default; never raises for a deny (only for a broken database).
    Denials are audited as `authz.denied` (S5's `audit.record`), critical allows as
    `authz.allowed_critical`."""
    deny = Decision.deny
    meta = PERMISSION_META.get(str(permission))
    if meta is None:                                                    # 0. catalogue
        LOG.error("identity: unknown permission %r checked", permission)
        return _finish(ctx, deny(str(permission), scope, DenyReason.UNKNOWN_PERMISSION))
    p = ctx.principal
    if not p.active:                                                    # 1. liveness
        return _finish(ctx, deny(meta.key, scope, DenyReason.PRINCIPAL_INACTIVE))
    if scope.org_id != p.org_id:
        return _finish(ctx, deny(meta.key, scope, DenyReason.NOT_FOUND))
    if meta.org_only and scope.kind == ScopeKind.PROJECT:
        return _finish(ctx, deny(meta.key, scope, DenyReason.ORG_ONLY_AT_PROJECT))
    cred = ctx.credential
    if cred is not None:
        if cred.principal_id != p.id or (cred.expires_at is not None and cred.expires_at <= _now()):
            return _finish(ctx, deny(meta.key, scope, DenyReason.CREDENTIAL_INVALID))
        if cred.project_ids is not None and not meta.org_only and (
                scope.project_id is None or scope.project_id not in cred.project_ids):
            return _finish(ctx, deny(meta.key, scope, DenyReason.OUT_OF_SCOPE, ("key_projects",)))
    if ctx.run_token is not None and not meta.delegable:               # agents: human-only keys
        return _finish(ctx, deny(meta.key, scope, DenyReason.NOT_DELEGABLE))

    with _epoch.connection(db) as conn:
        current = conn.execute(text(
            "SELECT (SELECT epoch FROM identity_settings WHERE org_id = :o), "
            "       (SELECT status FROM identity_principals WHERE id = :p AND org_id = :o)"),
            {"o": p.org_id, "p": p.id}).first()
        epoch, status = int(current[0] or 0), current[1]
        if p.kind == PrincipalKind.SYSTEM:                              # 2. system fixed sets
            if status is not None and status != "active":
                return _finish(ctx, deny(meta.key, scope, DenyReason.PRINCIPAL_INACTIVE))
            if meta.key in system_permissions(p.id, db=conn):
                return _finish(ctx, Decision.allow(meta.key, scope, (
                    Why(role_key=f"system:{p.id}", binding_id="system", binding_scope="org"),)))
            return _finish(ctx, deny(meta.key, scope, DenyReason.MISSING_PERMISSION))
        if status != "active":
            return _finish(ctx, deny(meta.key, scope, DenyReason.PRINCIPAL_INACTIVE))
        if meta.system_only:
            return _finish(ctx, deny(meta.key, scope, DenyReason.MISSING_PERMISSION))
        if cred is not None:
            live = conn.execute(text(
                "SELECT revoked_at, suspended_at, expires_at FROM identity_api_credentials "
                "WHERE id = :k AND principal_id = :p"), {"k": cred.id, "p": p.id}).first()
            if (live is None or live[0] is not None or live[1] is not None
                    or (live[2] is not None and live[2] <= _now())):
                return _finish(ctx, deny(meta.key, scope, DenyReason.CREDENTIAL_INVALID))
        if scope.kind == ScopeKind.PROJECT and not _project_in_org(conn, p.org_id, scope.project_id or ""):
            return _finish(ctx, deny(meta.key, scope, DenyReason.NOT_FOUND))
        if ctx.run_token is not None:                                   # 3a. agent runs: S3
            delegated = _delegate_run_token(ctx, meta.key, scope, conn)
            if delegated is not None:
                return delegated
        base = _base(conn, ctx, scope, epoch)                           # 3. the base set
        if base.deny is not None:
            return _finish(ctx, deny(meta.key, scope, base.deny[0], base.deny[1]))
        if meta.key not in base.permissions:
            if _invisible(meta.key, scope, base.permissions):
                return _finish(ctx, deny(meta.key, scope, DenyReason.NOT_FOUND))
            return _finish(ctx, deny(meta.key, scope, DenyReason.MISSING_PERMISSION,
                                     base.removed_by.get(meta.key, ())))
        if meta.step_up:                                                # 4. step-up
            fresh = step_up_fresh_until(ctx, window_seconds=_step_up_window_seconds(p.org_id, conn))
            if fresh is None or fresh <= _now():
                return _finish(ctx, deny(meta.key, scope, DenyReason.STEP_UP_REQUIRED))
        return _finish(ctx, Decision.allow(meta.key, scope, base.why.get(meta.key, ())))  # 5.


def _delegate_run_token(ctx: AuthContext, permission: str, scope: ResolvedScope,
                        conn: Any) -> Optional[Decision]:
    """On-behalf-of and autonomous agent runs are decided by S3's `tokens.decide` (the live
    intersection: the person's live access ∩ every ceiling in the chain ∩ grant ∩ task; never a
    non-delegable key), which audits its own denials in the lineage stream. None while S3's module
    has not landed: then this module's own intersection (`_base`) decides."""
    try:
        from . import tokens
    except ImportError:
        return None
    decide = getattr(tokens, "decide", None)
    if decide is None:
        return None
    decision = decide(ctx.run_token, permission, scope, db=conn)
    if not decision.allowed and decision.reason == DenyReason.MISSING_PERMISSION and \
            scope.object_kind and decision.narrowed_by == ():
        return Decision.deny(permission, scope, DenyReason.NOT_FOUND)
    return decision


#: The spec's name for the same function (IDENTITY.md §6.6).
authorize = check


def effective_permissions(subject: AuthContext | Principal, scope: ResolvedScope, *,
                          db: Any = None, with_why: bool = False) -> EffectivePermissions:
    """The union of role permissions bound to the principal or its groups at org scope or at
    `scope`'s project — narrowed by key scopes or the run token's intersection when `subject` is
    an AuthContext. Cached per (principal, scope, identity epoch).

    Step-up permissions are included (step-up is a freshness condition, reported as
    `step_up_fresh_until`); a run token never holds a non-delegable permission."""
    principal = subject.principal if isinstance(subject, AuthContext) else subject
    with _epoch.connection(db) as conn:
        epoch = _epoch.read(principal.org_id, db=conn)
        if scope.org_id != principal.org_id or not principal.active or (
                scope.kind == ScopeKind.PROJECT
                and not _project_in_org(conn, principal.org_id, scope.project_id or "")):
            return EffectivePermissions(frozenset(), scope, epoch)
        if principal.kind == PrincipalKind.SYSTEM:
            perms = system_permissions(principal.id, db=conn)
            return EffectivePermissions(perms, scope, epoch,
                                        datetime.max.replace(tzinfo=timezone.utc))
        if isinstance(subject, AuthContext):
            base = _base(conn, subject, scope, epoch)
            perms = frozenset() if base.deny else base.permissions
            if subject.run_token is not None:
                perms = frozenset(k for k in perms if PERMISSION_META[k].delegable)
            if subject.credential is not None and subject.credential.project_ids is not None:
                if scope.project_id is None or scope.project_id not in subject.credential.project_ids:
                    perms = frozenset(k for k in perms if PERMISSION_META[k].org_only)
            why = base.why
            fresh = step_up_fresh_until(
                subject, window_seconds=_step_up_window_seconds(principal.org_id, conn))
        else:
            held = _held(conn, principal.org_id, principal.id, scope, epoch)
            perms, why, fresh = held.permissions, held.why, None
        if scope.kind == ScopeKind.PROJECT:
            perms = frozenset(k for k in perms if not PERMISSION_META[k].org_only
                              or any(w.binding_scope == "org" for w in why.get(k, ())))
    return EffectivePermissions(
        permissions=perms, scope=scope, epoch=epoch, step_up_fresh_until=fresh,
        why={k: why.get(k, ()) for k in sorted(perms)} if with_why else {})


def explain(principal_id: str, permission: str, scope: ResolvedScope, *,
            db: Any = None) -> tuple[Why, ...]:
    """Every binding path that grants `permission` at `scope` (empty: not held). Feeds the
    console's "Effective access" view and every audited decision."""
    meta = PERMISSION_META.get(permission)
    if meta is None or (meta.org_only and scope.kind == ScopeKind.PROJECT):
        return ()
    with _epoch.connection(db) as conn:
        epoch = _epoch.read(scope.org_id, db=conn)
        return _held(conn, scope.org_id, principal_id, scope, epoch).why.get(permission, ())


def bump_epoch(org_id: str, *, db: Any, reason: str) -> int:
    """Increment identity_settings.epoch in the caller's transaction and NOTIFY identity_epoch.
    Every identity mutation calls this (§6.6)."""
    return _epoch.bump(org_id, db=db, reason=reason)


def holds_all(ctx: AuthContext, permissions: frozenset[str] | set[str], scope: ResolvedScope, *,
              db: Any = None) -> tuple[bool, frozenset[str]]:
    """The grant ceiling (§6.11): does the caller hold every one of `permissions` at `scope`?
    Returns (ok, the ones it lacks). Org-only permissions are compared only at org scope (A3)."""
    wanted = {k for k in permissions
              if not (scope.kind == ScopeKind.PROJECT and PERMISSION_META[k].org_only)}
    held = effective_permissions(ctx, scope, db=db).permissions
    missing = frozenset(wanted - held)
    return (not missing, missing)


def may_spawn(ctx: AuthContext, child_agent_id: str, *, db: Any = None) -> Decision:
    """A39: may the current actor of an agent run start `child_agent_id` as a sub-agent?
    The parent's Passport must allow spawning and name the child in `allowed_children`; the
    delegation grant (when it lists children) must too; and the child must not already be in the
    chain (a cycle) nor exceed the depth cap. The token service calls this before minting a
    child token (§7.7)."""
    scope = ResolvedScope.org_level(ctx.principal.org_id)
    rt = ctx.run_token
    label = "agent:spawn"
    if rt is None or rt.chain.current is None:
        return Decision.deny(label, scope, DenyReason.MISSING_PERMISSION, ("not_an_agent_run",))
    parent = rt.chain.current.agent_id
    if child_agent_id in rt.chain.agent_ids:
        return Decision.deny(label, scope, DenyReason.MISSING_PERMISSION, ("cycle",))
    with _epoch.connection(db) as conn:
        passport = conn.execute(text(
            "SELECT can_spawn, allowed_children, max_depth FROM identity_agent_passports "
            "WHERE agent_id = :a"), {"a": parent}).mappings().first()
        if passport is None or not passport["can_spawn"] or \
                child_agent_id not in (passport["allowed_children"] or []):
            return Decision.deny(label, scope, DenyReason.MISSING_PERMISSION,
                                 (f"passport:{parent}",))
        if rt.chain.depth + 1 > int(passport["max_depth"]):
            return Decision.deny(label, scope, DenyReason.MISSING_PERMISSION, ("depth",))
        if rt.grant_id:
            grant = conn.execute(text(
                "SELECT allow_subdelegation, allowed_children, max_depth, status "
                "FROM identity_delegation_grants WHERE id = :g AND org_id = :o"),
                {"g": rt.grant_id, "o": ctx.principal.org_id}).mappings().first()
            if grant is None or grant["status"] != "active":
                return Decision.deny(label, scope, DenyReason.GRANT_INACTIVE, (f"grant:{rt.grant_id}",))
            if not grant["allow_subdelegation"] or (
                    grant["allowed_children"] and child_agent_id not in grant["allowed_children"]):
                return Decision.deny(label, scope, DenyReason.MISSING_PERMISSION,
                                     (f"grant:{rt.grant_id}",))
            if rt.chain.depth + 1 > int(grant["max_depth"]):
                return Decision.deny(label, scope, DenyReason.MISSING_PERMISSION, ("depth",))
        child = _statuses(conn, ctx.principal.org_id, [child_agent_id]).get(child_agent_id)
        if child != "active":
            return Decision.deny(label, scope, DenyReason.CHAIN_INACTIVE, (child_agent_id,))
    return Decision(True, label, scope)
