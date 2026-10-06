"""Delegation grants: the revocable answer to "may this agent act for me, and how far?". Owned by S3.

IDENTITY.md §7.3, §7.4. A grant is a row in identity_delegation_grants (+ its permission rows):
  - standing ("for 30 days", max 90, "until revoked" only when policy allows it),
  - task-bound ("for this build only": `task_run_id`, ended with the run by `end_task_grants`),
  - schedule-bound (a standing grant a schedule names; the schedule pauses when it ends).

At creation: permissions ⊆ the person's LIVE permissions at every named project ∩ every named
agent's ceiling, all delegable; the GitHub resources pass the user-repo-access guard on
multi-person installs (D28). At every use (tokens.py) the same intersection is recomputed live,
so demoting the person, lowering a ceiling or suspending an agent narrows every run at its next
call. Only a person in a human session creates a grant (`delegation:grant` is never delegable).
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

from . import catalogue
from .delegation import (
    GRANT_DEFAULT_DAYS,
    GRANT_MAX_DAYS,
    HARD_MAX_DEPTH,
    DelegationGrant,
    GrantRequest,
    GrantStatus,
)
from .principal import AuthContext, Principal, PrincipalKind, PrincipalStatus

TASK_GRANT_MAX = timedelta(days=7)
PENDING_PREFIX = "pending:"
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
#: The GitHub App permission names a grant may carry, and their levels (read < write).
GITHUB_PERMISSIONS = frozenset({"contents", "pull_requests", "issues", "metadata", "workflows",
                                "checks", "statuses", "actions"})
LEVELS = {"none": 0, "read": 1, "write": 2}


class GrantRefused(Exception):
    def __init__(self, code: str, status: int = 403, detail: Optional[Mapping[str, Any]] = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.detail = dict(detail or {})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def policy_for(org_id: str, *, db: Any):
    """S5's policy, or the frozen defaults while S5's store is not built (the defaults ARE the
    documented policy; nothing here widens on a missing store)."""
    from . import policy

    try:
        return policy.get_policy(org_id, db=db)
    except NotImplementedError:
        return policy.IdentityPolicy()


def org_max_depth(org_id: str, *, db: Any) -> int:
    return max(0, min(int(policy_for(org_id, db=db).org_max_depth), HARD_MAX_DEPTH))


def principal(db: Any, principal_id: str) -> Optional[Principal]:
    from sqlalchemy import text

    row = db.execute(text("SELECT id, kind, org_id, status, display_name, network_bound FROM identity_principals "
                          "WHERE id = :id"), {"id": principal_id}).mappings().first()
    if row is None:
        return None
    return Principal(row["id"], PrincipalKind(row["kind"]), row["org_id"], PrincipalStatus(row["status"]),
                     row["display_name"], network_bound=row["network_bound"])


def person_live(db: Any, person: Principal, project_id: Optional[str]) -> frozenset[str]:
    """The person's permissions RIGHT NOW at the project (S2's effective_permissions, which is
    cached per epoch and invalidated by every role change)."""
    from . import authz
    from .scope import ResolvedScope

    scope = (ResolvedScope.in_project(person.org_id, project_id) if project_id
             else ResolvedScope.org_level(person.org_id))
    return frozenset(authz.effective_permissions(person, scope, db=db).permissions)


# ---- read --------------------------------------------------------------------------------------------

def _from_row(db: Any, row: Mapping[str, Any]) -> DelegationGrant:
    from sqlalchemy import text

    perms = frozenset(db.execute(text("SELECT permission_key FROM identity_delegation_grant_permissions "
                                      "WHERE grant_id = :g"), {"g": row["id"]}).scalars().all())
    return DelegationGrant(
        id=row["id"], org_id=row["org_id"], user_principal_id=row["user_principal_id"],
        agent_ids=tuple(row["agent_ids"] or ()), permissions=perms, project_ids=tuple(row["project_ids"] or ()),
        resources=row["resources"] or {}, allow_subdelegation=row["allow_subdelegation"],
        allowed_children=tuple(row["allowed_children"] or ()), max_depth=row["max_depth"],
        status=GrantStatus(row["status"]), created_at=row["created_at"], expires_at=row["expires_at"],
        task_run_id=row["task_run_id"], parent_grant_id=row["parent_grant_id"], purpose=row["purpose"] or "")


def load(grant_id: Optional[str], *, db: Any) -> Optional[DelegationGrant]:
    from sqlalchemy import text

    if not grant_id:
        return None
    row = db.execute(text("SELECT * FROM identity_delegation_grants WHERE id = :id"), {"id": grant_id}).mappings().first()
    return _from_row(db, row) if row else None


def for_person(person_id: str, *, db: Any) -> list[DelegationGrant]:
    from sqlalchemy import text

    rows = db.execute(text("SELECT * FROM identity_delegation_grants WHERE user_principal_id = :u "
                           "ORDER BY created_at DESC"), {"u": person_id}).mappings().all()
    return [_from_row(db, r) for r in rows]


def is_live(grant: Optional[DelegationGrant], *, now: Optional[datetime] = None) -> bool:
    if grant is None or grant.status != GrantStatus.ACTIVE:
        return False
    return grant.expires_at is None or grant.expires_at > (now or _now())


def allows_as_user(grant_id: Optional[str], provider: str, *, db: Any) -> bool:
    """True only when the grant says "act as me on <provider>" explicitly."""
    grant = load(grant_id, db=db)
    if not is_live(grant):
        return False
    res = (grant.resources or {}).get(provider) or {}
    return bool(isinstance(res, Mapping) and res.get("as_user") is True)


def run_matches(grant: DelegationGrant, run_id: str) -> bool:
    """A task-bound grant covers its run and that run's children (`<run>/<child>`) only."""
    if not grant.task_run_id:
        return True
    return run_id == grant.task_run_id or run_id.startswith(grant.task_run_id + "/")


def names_agent(grant: DelegationGrant, agent_id: str) -> bool:
    return agent_id in grant.agent_ids


def child_allowed(grant: DelegationGrant, child_agent_id: str, child_role_key: str) -> bool:
    return grant.allow_subdelegation and (child_agent_id in grant.allowed_children
                                          or child_role_key in grant.allowed_children)


def github_resources(grant: Optional[DelegationGrant]) -> Mapping[str, Any]:
    if grant is None:
        return {}
    res = (grant.resources or {}).get("github") or {}
    return res if isinstance(res, Mapping) else {}


# ---- create ------------------------------------------------------------------------------------------

def _validate_github(resources: Mapping[str, Any]) -> dict[str, Any]:
    gh = resources.get("github")
    if gh is None:
        return {}
    if not isinstance(gh, Mapping):
        raise GrantRefused("bad_resources", 422)
    repos = gh.get("repos") or []
    if not isinstance(repos, list) or not all(isinstance(r, str) and REPO_PATTERN.match(r) for r in repos):
        raise GrantRefused("bad_repos", 422)
    out: dict[str, Any] = {"repos": sorted(set(repos))}
    for key, value in gh.items():
        if key in ("repos",):
            continue
        if key == "as_user":
            out["as_user"] = value is True
            continue
        if key not in GITHUB_PERMISSIONS or value not in LEVELS:
            raise GrantRefused("bad_github_permission", 422, {"key": key})
        out[key] = value
    return out


def multi_person_install(org_id: str, *, db: Any) -> bool:
    """D28: more than one ACTIVE human principal (the network-bridge and legacy principals do not
    count). On a one-person install the guard is off: the only person cannot be confused-deputied."""
    from sqlalchemy import text

    n = db.execute(text(
        "SELECT count(*) FROM identity_principals WHERE org_id = :o AND kind = 'user' AND status = 'active' "
        "AND NOT network_bound AND id NOT IN ('usr_legacy_shared','usr_network_operator')"), {"o": org_id}).scalar_one()
    return n > 1


def repo_guard_active(org_id: str, *, db: Any) -> bool:
    setting = policy_for(org_id, db=db).github_require_user_repo_access
    return multi_person_install(org_id, db=db) if setting is None else bool(setting)


_GUARD_CACHE: dict[tuple[str, str, str], tuple[bool, float]] = {}
GUARD_TTL_SECONDS = 15 * 60


def check_user_repo_access(org_id: str, person_id: str, gh: Mapping[str, Any], *, db: Any,
                           use_cache: bool = True) -> None:
    """The confused-deputy guard: on multi-person installs the person must have at least the
    granted access on GitHub, checked with THEIR linked GitHub identity. Raises GrantRefused."""
    if not gh.get("repos") or not repo_guard_active(org_id, db=db):
        return
    from . import github_app

    needs_write = any(LEVELS.get(str(v), 0) >= 2 for k, v in gh.items() if k in GITHUB_PERMISSIONS)
    need = "push" if needs_write else "pull"
    for repo in gh["repos"]:
        key = (person_id, repo, need)
        hit = _GUARD_CACHE.get(key)
        if use_cache and hit and time.monotonic() - hit[1] < GUARD_TTL_SECONDS:
            ok = hit[0]
        else:
            ok = github_app.user_has_repo_access(person_id, repo, need, db=db)
            _GUARD_CACHE[key] = (ok, time.monotonic())
        if not ok:
            raise GrantRefused("github_access_lacking", 403, {"repo": repo, "needs": need})


def clear_guard_cache() -> None:
    _GUARD_CACHE.clear()


def create_grant(ctx: AuthContext, request: GrantRequest, *, db: Any) -> DelegationGrant:
    from sqlalchemy import text

    from . import authz, lineage, passports
    from .ids import new_id
    from .scope import ResolvedScope

    # Who: a person, in a human session. An agent's token never creates or widens a grant.
    if ctx is None or not ctx.is_human_session:
        raise GrantRefused("people_only", 403)
    person = ctx.principal
    decision = authz.check(ctx, "delegation:grant", ResolvedScope.org_level(person.org_id), db=db)
    if not decision.allowed:
        raise GrantRefused(decision.error_body().get("error", "forbidden"), decision.status,
                           {"permission": "delegation:grant"})
    live_person = principal(db, person.id)
    if live_person is None or not live_person.active:
        raise GrantRefused("person_inactive", 401)

    perms = frozenset(request.permissions)
    if not perms:
        raise GrantRefused("empty_permissions", 422)
    for key in perms:
        if not catalogue.is_known(key):
            raise GrantRefused("unknown_permission", 422, {"permission": key})
        if not catalogue.meta(key).delegable:
            raise GrantRefused("not_delegable", 403, {"permission": key})
    if not request.agent_ids:
        raise GrantRefused("no_agents", 422)
    if not request.project_ids:
        raise GrantRefused("no_projects", 422)
    pol = policy_for(person.org_id, db=db)
    max_depth = int(request.max_depth)
    if not 0 <= max_depth <= min(HARD_MAX_DEPTH, int(pol.org_max_depth)):
        raise GrantRefused("bad_max_depth", 422)
    if request.allowed_children and not request.allow_subdelegation:
        raise GrantRefused("children_without_subdelegation", 422)

    # Every named agent: an active Passport in this org; the permissions fit under its ceiling.
    for agent_id in list(request.agent_ids) + list(request.allowed_children):
        p = passports.get(agent_id, db=db)
        if p is None and agent_id in request.allowed_children:
            continue                                   # a role key in the child allow-list
        if p is None or p.org_id != person.org_id:
            raise GrantRefused("unknown_agent", 404, {"agent": agent_id})
        if not p.active:
            raise GrantRefused("agent_inactive", 409, {"agent": agent_id})
        if agent_id in request.agent_ids and not perms <= p.ceiling:
            raise GrantRefused("exceeds_ceiling", 403, {"agent": agent_id,
                                                       "permissions": sorted(perms - p.ceiling)})
    # ...and under the person's own live access at every project.
    for project_id in request.project_ids:
        missing = perms - person_live(db, live_person, project_id)
        if missing:
            raise GrantRefused("exceeds_your_access", 403, {"project": project_id,
                                                          "permissions": sorted(missing)})

    resources = dict(request.resources or {})
    gh = _validate_github(resources)
    if gh:
        resources["github"] = gh
        check_user_repo_access(person.org_id, person.id, gh, db=db)

    # Expiry: default 30 d, max 90 d; "until revoked" only when policy allows it.
    now = _now()
    max_days = min(int(pol.delegation_max_days), GRANT_MAX_DAYS)
    expires_at = request.expires_at
    if request.task_run_id is not None and expires_at is None:
        # "For this build only" ends with the run; an unclaimed one (consent given before the run
        # existed: `pending:`) never outlives a week.
        expires_at = now + TASK_GRANT_MAX
    if expires_at is None and not pol.delegation_allow_until_revoked:
        expires_at = now + timedelta(days=int(pol.delegation_default_days or GRANT_DEFAULT_DAYS))
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= now:
            raise GrantRefused("expired", 422)
        if expires_at > now + timedelta(days=max_days):
            raise GrantRefused("too_long", 422, {"max_days": max_days})

    grant_id = new_id("dlg")
    import json

    db.execute(text(
        "INSERT INTO identity_delegation_grants (id, org_id, user_principal_id, agent_ids, project_ids, resources, "
        " allow_subdelegation, allowed_children, max_depth, task_run_id, purpose, status, expires_at) VALUES "
        "(:id, :org, :u, :agents, :projects, CAST(:res AS JSONB), :sub, :children, :depth, :task, :purpose, "
        " 'active', :exp)"),
        {"id": grant_id, "org": person.org_id, "u": person.id, "agents": list(request.agent_ids),
         "projects": list(request.project_ids), "res": json.dumps(resources), "sub": request.allow_subdelegation,
         "children": list(request.allowed_children), "depth": max_depth, "task": request.task_run_id,
         "purpose": request.purpose[:500], "exp": expires_at})
    for key in sorted(perms):
        db.execute(text("INSERT INTO identity_delegation_grant_permissions (grant_id, permission_key) "
                        "VALUES (:g, :k)"), {"g": grant_id, "k": key})
    grant = load(grant_id, db=db)
    lineage.grant_event(db, grant, "delegation.created", ctx=ctx,
                        details={"agents": list(grant.agent_ids), "permissions": sorted(perms),
                                 "projects": list(grant.project_ids), "task_run": grant.task_run_id,
                                 "expires_at": expires_at.isoformat() if expires_at else None})
    return grant


def mark_first_use(db: Any, grant: DelegationGrant) -> None:
    from sqlalchemy import text

    from . import lineage

    was = db.execute(text("SELECT last_used_at FROM identity_delegation_grants WHERE id = :g FOR UPDATE"),
                     {"g": grant.id}).scalar()
    db.execute(text("UPDATE identity_delegation_grants SET last_used_at = now() WHERE id = :g"), {"g": grant.id})
    if was is None:
        lineage.grant_event(db, grant, "delegation.used_first")


def descendants(db: Any, grant_ids: Iterable[str]) -> list[str]:
    """Every child grant, transitively (parent_grant_id)."""
    from sqlalchemy import text

    rows = db.execute(text(
        "WITH RECURSIVE d(id) AS (SELECT id FROM identity_delegation_grants WHERE parent_grant_id = ANY(:ids) "
        " UNION SELECT g.id FROM identity_delegation_grants g JOIN d ON g.parent_grant_id = d.id) "
        "SELECT id FROM d"), {"ids": list(grant_ids)}).scalars().all()
    return list(rows)


def expire_due(*, db: Any, now: Optional[datetime] = None) -> list[str]:
    """Expire grants past their time, through the cascade (runs halt, tokens die)."""
    from sqlalchemy import text

    from . import cascade

    rows = db.execute(text("SELECT id, org_id FROM identity_delegation_grants WHERE status = 'active' "
                           "AND expires_at IS NOT NULL AND expires_at <= :now"), {"now": now or _now()}).all()
    for gid, org in rows:
        cascade.revoke_cascade(cascade.CascadeTrigger.GRANT_EXPIRED, gid, actor=cascade.system_actor(org),
                               reason="expired", db=db)
    return [r[0] for r in rows]


def end_task_grants(run_id: str, *, db: Any, org_id: str) -> list[str]:
    """"For this build only" ends with the build: called by the run supervisor at run end."""
    from sqlalchemy import text

    from . import cascade

    ids = db.execute(text("SELECT id FROM identity_delegation_grants WHERE task_run_id = :r AND status = 'active'"),
                     {"r": run_id}).scalars().all()
    for gid in ids:
        cascade.revoke_cascade(cascade.CascadeTrigger.GRANT_EXPIRED, gid, actor=cascade.system_actor(org_id),
                               reason="task ended", db=db)
    return list(ids)


# ---- changing a grant: extend (time only) and narrow (never wider) ----------------------------------------

def _require_live_grant(grant_id: str, db: Any) -> DelegationGrant:
    g = load(grant_id, db=db)
    if g is None:
        raise GrantRefused("not_found", 404)
    if g.status != GrantStatus.ACTIVE:
        raise GrantRefused("revoked_is_terminal", 409)
    return g


def extend_grant(ctx: AuthContext, grant_id: str, days: int, *, db: Any) -> DelegationGrant:
    """Only the person who granted it, in a human session. Extending re-authorizes, so the
    permissions are re-checked against their live access and every ceiling (no stale widening)."""
    from sqlalchemy import text

    from . import audit, lineage, passports

    if ctx is None or not ctx.is_human_session:
        raise GrantRefused("people_only", 403)
    g = _require_live_grant(grant_id, db)
    if g.user_principal_id != ctx.principal.id or g.org_id != ctx.principal.org_id:
        raise GrantRefused("forbidden", 403, {"permission": "delegation:grant"})
    if g.task_run_id:
        raise GrantRefused("task_grants_end_with_their_run", 409)
    pol = policy_for(g.org_id, db=db)
    days = max(1, min(int(days), int(pol.delegation_max_days), GRANT_MAX_DAYS))
    person = principal(db, g.user_principal_id)
    for project_id in g.project_ids:
        missing = g.permissions - person_live(db, person, project_id)
        if missing:
            raise GrantRefused("exceeds_your_access", 403, {"project": project_id, "permissions": sorted(missing)})
    for agent_id in g.agent_ids:
        p = passports.get(agent_id, db=db)
        if p is None or not p.active:
            raise GrantRefused("agent_inactive", 409, {"agent": agent_id})
        if not g.permissions <= p.ceiling:
            raise GrantRefused("exceeds_ceiling", 403, {"agent": agent_id, "permissions": sorted(g.permissions - p.ceiling)})
    new_exp = _now() + timedelta(days=days)
    db.execute(text("UPDATE identity_delegation_grants SET expires_at = :e WHERE id = :g"), {"e": new_exp, "g": g.id})
    event = "delegation.extended" if "delegation.extended" in audit.EVENT_TYPES else "delegation.created"
    after = load(g.id, db=db)
    lineage.grant_event(db, after, event, ctx=ctx, details={"change": "extended", "days": days,
                                                            "expires_at": new_exp.isoformat()})
    return after


def narrow_grant(ctx: AuthContext, grant_id: str, permissions: frozenset[str], *, db: Any) -> DelegationGrant:
    """The new set must be a strict-or-equal SUBSET (422 `narrowing_only` otherwise). The grantor or
    `delegation:revoke_any`. Every live token narrows at its next call (grants are read live)."""
    from sqlalchemy import text

    from . import authz, lineage
    from .scope import ResolvedScope

    if ctx is None or ctx.run_token is not None:
        raise GrantRefused("people_only", 403)
    g = _require_live_grant(grant_id, db)
    if g.org_id != ctx.principal.org_id:
        raise GrantRefused("not_found", 404)
    if g.user_principal_id != ctx.principal.id:
        decision = authz.check(ctx, "delegation:revoke_any", ResolvedScope.org_level(g.org_id), db=db)
        if not decision.allowed:
            raise GrantRefused("forbidden", 403, {"permission": "delegation:revoke_any"})
    asked = frozenset(permissions)
    widened = sorted(asked - g.permissions)
    if widened:
        raise GrantRefused("narrowing_only", 422, {"permission": widened[0]})
    if not asked:
        raise GrantRefused("empty_permissions", 422)
    dropped = g.permissions - asked
    if dropped:
        db.execute(text("DELETE FROM identity_delegation_grant_permissions WHERE grant_id = :g AND permission_key = ANY(:p)"),
                   {"g": g.id, "p": sorted(dropped)})
    after = load(g.id, db=db)
    lineage.grant_event(db, after, "delegation.narrowed", ctx=ctx, details={"removed": sorted(dropped)})
    return after


# ---- just-in-time consent: does a standing or task grant cover this work? -----------------------------

def covers(g: DelegationGrant, *, agent_ids: Iterable[str], permissions: frozenset[str], project_id: Optional[str],
           repos: Iterable[str], run_id: Optional[str]) -> bool:
    if not is_live(g):
        return False
    if not set(agent_ids) <= set(g.agent_ids) or not permissions <= g.permissions:
        return False
    if project_id and project_id not in g.project_ids:
        return False
    if set(repos) - set(github_resources(g).get("repos") or ()):
        return False
    if g.task_run_id:
        if g.task_run_id.startswith(PENDING_PREFIX):
            return True                          # claimable by this person's next matching run
        return run_id is not None and run_matches(g, run_id)
    return True


def find_covering(person_id: str, *, agent_ids: Iterable[str], permissions: frozenset[str],
                  project_id: Optional[str], repos: Iterable[str], run_id: Optional[str], db: Any
                  ) -> Optional[DelegationGrant]:
    """Prefer a task grant for this very run, then a standing grant, then an unclaimed pending one
    (which is CLAIMED for `run_id` here, so it can never cover a second run)."""
    from sqlalchemy import text

    agent_ids, repos = list(agent_ids), list(repos)
    found = [g for g in for_person(person_id, db=db)
             if covers(g, agent_ids=agent_ids, permissions=permissions, project_id=project_id, repos=repos, run_id=run_id)]
    found.sort(key=lambda g: (0 if g.task_run_id and not g.task_run_id.startswith(PENDING_PREFIX)
                              else 1 if not g.task_run_id else 2))
    for g in found:
        if g.task_run_id and g.task_run_id.startswith(PENDING_PREFIX):
            if run_id is None:
                continue
            claimed = db.execute(text("UPDATE identity_delegation_grants SET task_run_id = :r WHERE id = :g "
                                      "AND task_run_id = :old RETURNING id"),
                                 {"r": run_id, "g": g.id, "old": g.task_run_id}).scalar()
            if not claimed:
                continue
            return load(g.id, db=db)
        return g
    return None
