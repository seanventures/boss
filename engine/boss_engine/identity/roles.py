"""Access roles: the five built-ins (read-only) and custom roles (created, cloned, edited, reverted,
deleted). Stream S2. IDENTITY.md §6.9–§6.11.

Every write runs in the caller's transaction (`db=`), applies the grant ceiling (nobody creates or
edits a role holding more than they hold, at every scope the role is bound), writes a role version,
audits, and bumps the identity epoch, so the next request anywhere sees the change.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from sqlalchemy import text

from . import authz
from .catalogue import HUMAN_ROLES
from .ids import new_id
from .permissions_gen import PERMISSION_META
from .principal import AuthContext
from .scope import ResolvedScope

ROLE_KEY = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
SCOPES = ("org", "project")


@dataclass
class AccessError(Exception):
    """A refused identity administration call. `status` is the HTTP status; `body` the error body
    (never more than the caller may know)."""
    status: int
    error: str
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.detail or self.error)

    def body(self) -> dict[str, Any]:
        return {"error": self.error, **({"detail": self.detail} if self.detail else {}), **self.extra}


def forbidden(permission: str) -> AccessError:
    return AccessError(403, "forbidden", extra={"permission": permission})


def org_scope(ctx: AuthContext) -> ResolvedScope:
    return ResolvedScope.org_level(ctx.principal.org_id)


def scope_of(org_id: str, scope_type: str, scope_id: str) -> ResolvedScope:
    if scope_type == "org":
        return ResolvedScope.org_level(org_id)
    return ResolvedScope.in_project(org_id, scope_id)


def require(ctx: AuthContext, permission: str, scope: ResolvedScope, *, db: Any) -> None:
    """Re-check a permission at the exact scope a write lands on (the route's declaration checked
    the route's scope; a binding at project X must also hold at project X)."""
    decision = authz.check(ctx, permission, scope, db=db)
    if not decision.allowed:
        raise AccessError(decision.status, decision.error_body().get("error", "forbidden"),
                          extra={k: v for k, v in decision.error_body().items() if k != "error"})


def require_ceiling(ctx: AuthContext, permissions: Iterable[str], scope: ResolvedScope, *,
                    db: Any) -> None:
    """§6.11: the caller holds every one of `permissions` at `scope`, or 403 naming what it lacks."""
    ok, missing = authz.holds_all(ctx, frozenset(permissions), scope, db=db)
    if not ok:
        raise AccessError(403, "forbidden", "You can't grant access you don't hold.",
                          {"reason": "grant_ceiling", "missing": sorted(missing),
                           "scope": scope.label()})


def audit_event(ctx: AuthContext, event_type: str, *, db: Any, target_type: str, target_id: str,
                scope: Optional[str] = None, details: Optional[dict] = None) -> None:
    """Record an admin event in the caller's transaction (A76: a failed write rolls the change
    back). Skipped only while S5's audit.record is still a stub."""
    from . import audit

    event = audit.SecurityEvent.from_context(ctx, event_type, audit.Outcome.SUCCESS,
                                             target_type=target_type, target_id=target_id,
                                             scope=scope, details=details or {})
    try:
        audit.record(event, db=db)
    except NotImplementedError:
        pass


# ---- reads ---------------------------------------------------------------------------------------

def _role_row(db: Any, org_id: str, role_id: str) -> Optional[dict]:
    row = db.execute(text(
        "SELECT id, org_id, key, name, description, builtin, system, bindable_scopes, mfa_required, "
        "version, cloned_from, created_by, created_at, updated_at FROM identity_access_roles "
        "WHERE id = :r AND (org_id IS NULL OR org_id = :o)"), {"r": role_id, "o": org_id}
    ).mappings().first()
    return dict(row) if row else None


def permissions_of(db: Any, role_id: str) -> frozenset[str]:
    rows = db.execute(text("SELECT permission_key FROM identity_access_role_permissions "
                           "WHERE role_id = :r"), {"r": role_id}).all()
    return frozenset(str(r[0]) for r in rows)


def _present(db: Any, row: dict) -> dict:
    perms = sorted(permissions_of(db, row["id"]))
    bound = db.execute(text("SELECT count(*) FROM identity_role_bindings WHERE role_id = :r"),
                       {"r": row["id"]}).scalar()
    return {"id": row["id"], "key": row["key"], "name": row["name"],
            "description": row["description"], "builtin": row["builtin"], "system": row["system"],
            "bindable_scopes": list(row["bindable_scopes"]), "mfa_required": row["mfa_required"],
            "version": row["version"], "cloned_from": row["cloned_from"],
            "permissions": perms, "binding_count": int(bound or 0)}


def get_role(db: Any, org_id: str, role_id: str) -> dict:
    row = _role_row(db, org_id, role_id)
    if row is None:
        raise AccessError(404, "not_found")
    return _present(db, row)


def list_roles(db: Any, org_id: str) -> list[dict]:
    rows = db.execute(text(
        "SELECT id, org_id, key, name, description, builtin, system, bindable_scopes, mfa_required, "
        "version, cloned_from, created_by, created_at, updated_at FROM identity_access_roles "
        "WHERE (org_id IS NULL OR org_id = :o) ORDER BY builtin DESC, system, key"),
        {"o": org_id}).mappings().all()
    return [_present(db, dict(r)) for r in rows]


def version(db: Any, org_id: str, role_id: str, number: int) -> dict:
    get_role(db, org_id, role_id)
    row = db.execute(text("SELECT version, permissions, changed_by, changed_at FROM "
                          "identity_access_role_versions WHERE role_id = :r AND version = :v"),
                     {"r": role_id, "v": int(number)}).mappings().first()
    if row is None:
        raise AccessError(404, "not_found")
    return dict(row)


def versions(db: Any, org_id: str, role_id: str) -> list[dict]:
    get_role(db, org_id, role_id)
    rows = db.execute(text("SELECT version, permissions, changed_by, changed_at FROM "
                           "identity_access_role_versions WHERE role_id = :r ORDER BY version DESC"),
                      {"r": role_id}).mappings().all()
    return [dict(r) for r in rows]


# ---- validation ------------------------------------------------------------------------------------

def validate_permissions(keys: Iterable[str]) -> frozenset[str]:
    """Only catalogue keys; no wildcards; never a system-only permission (§6.2). 422 otherwise."""
    out: set[str] = set()
    for key in keys:
        meta = PERMISSION_META.get(str(key))
        if meta is None:
            raise AccessError(422, "unknown_permission", extra={"permission": str(key)[:80]})
        if meta.system_only:
            raise AccessError(422, "system_only_permission", extra={"permission": meta.key})
        out.add(meta.key)
    return frozenset(out)


def validate_scopes(permissions: frozenset[str], scopes: Iterable[str]) -> tuple[str, ...]:
    chosen = tuple(s for s in SCOPES if s in set(scopes))
    if not chosen or set(scopes) - set(SCOPES):
        raise AccessError(422, "invalid_bindable_scopes", "bindable_scopes is a subset of org, project")
    if "project" in chosen and permissions and all(PERMISSION_META[k].org_only for k in permissions):
        # amendment A3: a role made only of org permissions does nothing at a project
        raise AccessError(422, "org_only_role", "A role of only org-wide permissions binds at org only.")
    return chosen


def _require_critical_confirmed(added: frozenset[str], confirmed: Iterable[str]) -> None:
    critical = sorted(k for k in added if PERMISSION_META[k].critical)
    if critical and set(critical) - set(confirmed or ()):
        raise AccessError(422, "confirmation_required",
                          "Critical permissions need a typed confirmation.",
                          {"critical": critical})


def _bound_scopes(db: Any, role_id: str, org_id: str) -> list[ResolvedScope]:
    rows = db.execute(text("SELECT DISTINCT scope_type, scope_id FROM identity_role_bindings "
                           "WHERE role_id = :r AND org_id = :o"), {"r": role_id, "o": org_id}).all()
    scopes = [ResolvedScope.org_level(org_id)]
    scopes += [scope_of(org_id, r[0], r[1]) for r in rows if r[0] == "project"]
    return scopes


# ---- writes ----------------------------------------------------------------------------------------

def _write_version(db: Any, role_id: str, version: int, permissions: frozenset[str],
                   ctx: AuthContext) -> None:
    db.execute(text("INSERT INTO identity_access_role_versions (role_id, version, permissions, "
                    "changed_by) VALUES (:r, :v, :p, :b)"),
               {"r": role_id, "v": version, "p": sorted(permissions), "b": ctx.principal.id})


def _set_permissions(db: Any, role_id: str, permissions: frozenset[str]) -> None:
    db.execute(text("DELETE FROM identity_access_role_permissions WHERE role_id = :r"), {"r": role_id})
    for key in sorted(permissions):
        db.execute(text("INSERT INTO identity_access_role_permissions (role_id, permission_key) "
                        "VALUES (:r, :k)"), {"r": role_id, "k": key})


def create_role(db: Any, ctx: AuthContext, *, key: str, name: str, description: str = "",
                permissions: Iterable[str], bindable_scopes: Iterable[str] = ("org", "project"),
                confirm_critical: Iterable[str] = (), cloned_from: Optional[str] = None) -> dict:
    org = ctx.principal.org_id
    if not ROLE_KEY.match(key or "") or key in HUMAN_ROLES:
        raise AccessError(422, "invalid_key", "A role key is lowercase snake_case and not a built-in's.")
    if not (name or "").strip():
        raise AccessError(422, "invalid_name")
    perms = validate_permissions(permissions)
    scopes = validate_scopes(perms, bindable_scopes)
    require_ceiling(ctx, perms, org_scope(ctx), db=db)              # A32
    _require_critical_confirmed(perms, confirm_critical)
    if db.execute(text("SELECT 1 FROM identity_access_roles WHERE org_id = :o AND key = :k"),
                  {"o": org, "k": key}).first():
        raise AccessError(409, "role_exists")
    role_id = new_id("arl")
    db.execute(text(
        "INSERT INTO identity_access_roles (id, org_id, key, name, description, builtin, system, "
        "bindable_scopes, mfa_required, version, cloned_from, created_by) VALUES "
        "(:id, :o, :k, :n, :d, false, false, :s, false, 1, :c, :b)"),
        {"id": role_id, "o": org, "k": key, "n": name.strip(), "d": description or "",
         "s": list(scopes), "c": cloned_from, "b": ctx.principal.id})
    _set_permissions(db, role_id, perms)
    _write_version(db, role_id, 1, perms, ctx)
    audit_event(ctx, "access_role.created", db=db, target_type="access_role", target_id=role_id,
                details={"key": key, "permissions": sorted(perms), "cloned_from": cloned_from})
    authz.bump_epoch(org, db=db, reason="access_role.created")
    return get_role(db, org, role_id)


def clone_role(db: Any, ctx: AuthContext, source_role_id: str, *, key: str, name: str,
               description: Optional[str] = None, confirm_critical: Iterable[str] = ()) -> dict:
    source = get_role(db, ctx.principal.org_id, source_role_id)
    if source["system"]:
        raise AccessError(404, "not_found")
    return create_role(db, ctx, key=key, name=name,
                       description=source["description"] if description is None else description,
                       permissions=source["permissions"],
                       bindable_scopes=source["bindable_scopes"],
                       confirm_critical=confirm_critical, cloned_from=source["id"])


def _editable(db: Any, org_id: str, role_id: str) -> dict:
    row = _role_row(db, org_id, role_id)
    if row is None or row["system"]:
        raise AccessError(404, "not_found")
    if row["builtin"] or row["org_id"] is None:
        raise AccessError(409, "builtin_read_only", "Built-in roles are read-only. Clone one instead.")
    return row


def update_role(db: Any, ctx: AuthContext, role_id: str, *, name: Optional[str] = None,
                description: Optional[str] = None, permissions: Optional[Iterable[str]] = None,
                bindable_scopes: Optional[Iterable[str]] = None,
                confirm_critical: Iterable[str] = ()) -> dict:
    org = ctx.principal.org_id
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"identity_role:{role_id}"})
    row = _editable(db, org, role_id)
    old = permissions_of(db, role_id)
    new = old if permissions is None else validate_permissions(permissions)
    scopes = validate_scopes(new, row["bindable_scopes"] if bindable_scopes is None else bindable_scopes)
    for scope in _bound_scopes(db, role_id, org):                   # A35: old ∪ new, everywhere bound
        require_ceiling(ctx, old | new, scope, db=db)
    _require_critical_confirmed(new - old, confirm_critical)
    in_use = {r[0] for r in db.execute(text(
        "SELECT DISTINCT scope_type FROM identity_role_bindings WHERE role_id = :r"), {"r": role_id}).all()}
    if in_use - set(scopes):
        raise AccessError(409, "scope_in_use", "Remove the bindings at that scope first.")
    version = int(row["version"]) + (1 if new != old else 0)
    db.execute(text(
        "UPDATE identity_access_roles SET name = :n, description = :d, bindable_scopes = :s, "
        "version = :v, updated_at = now() WHERE id = :r"),
        {"n": (name or row["name"]).strip(), "d": row["description"] if description is None else description,
         "s": list(scopes), "v": version, "r": role_id})
    if new != old:
        _set_permissions(db, role_id, new)
        _write_version(db, role_id, version, new, ctx)
    audit_event(ctx, "access_role.updated", db=db, target_type="access_role", target_id=role_id,
                details={"added": sorted(new - old), "removed": sorted(old - new), "version": version})
    authz.bump_epoch(org, db=db, reason="access_role.updated")
    return get_role(db, org, role_id)


def revert_role(db: Any, ctx: AuthContext, role_id: str, version: int, *,
                confirm_critical: Iterable[str] = ()) -> dict:
    """A revert is a new save of an old version's permissions, under the same checks."""
    row = db.execute(text("SELECT permissions FROM identity_access_role_versions "
                          "WHERE role_id = :r AND version = :v"), {"r": role_id, "v": int(version)}).first()
    if row is None:
        raise AccessError(404, "not_found")
    _editable(db, ctx.principal.org_id, role_id)
    return update_role(db, ctx, role_id, permissions=list(row[0]), confirm_critical=confirm_critical)


def delete_role(db: Any, ctx: AuthContext, role_id: str) -> None:
    org = ctx.principal.org_id
    row = _editable(db, org, role_id)
    bound = db.execute(text("SELECT count(*) FROM identity_role_bindings WHERE role_id = :r"),
                       {"r": role_id}).scalar()
    if bound:
        raise AccessError(409, "role_in_use", "Reassign its bindings first.", {"bindings": int(bound)})
    require_ceiling(ctx, permissions_of(db, role_id), org_scope(ctx), db=db)
    db.execute(text("DELETE FROM identity_access_roles WHERE id = :r"), {"r": role_id})
    audit_event(ctx, "access_role.deleted", db=db, target_type="access_role", target_id=role_id,
                details={"key": row["key"]})
    authz.bump_epoch(org, db=db, reason="access_role.deleted")


def preview(db: Any, org_id: str, role_id: str, permissions: Iterable[str]) -> dict:
    """The diff and impact of saving `permissions` on a role (§6.10): who gains or loses what,
    directly and through groups. Changes nothing."""
    get_role(db, org_id, role_id)
    old = permissions_of(db, role_id)
    new = validate_permissions(permissions)
    affected = db.execute(text("""
        SELECT b.scope_type, b.scope_id, b.subject_type, b.subject_id, g.name AS group_name,
               COALESCE(m.principal_id, b.subject_id) AS principal_id
          FROM identity_role_bindings b
          LEFT JOIN identity_groups g ON b.subject_type = 'group' AND g.id = b.subject_id
          LEFT JOIN identity_group_members m ON b.subject_type = 'group' AND m.group_id = b.subject_id
         WHERE b.role_id = :r AND b.org_id = :o"""), {"r": role_id, "o": org_id}).mappings().all()
    principals = sorted({a["principal_id"] for a in affected if a["principal_id"]})
    agents = db.execute(text(
        "SELECT DISTINCT g.id FROM identity_delegation_grants g "
        "WHERE g.org_id = :o AND g.status = 'active' AND g.user_principal_id = ANY(:p)"),
        {"o": org_id, "p": principals}).all() if principals else []
    return {"added": sorted(new - old), "removed": sorted(old - new),
            "critical_added": sorted(k for k in new - old if PERMISSION_META[k].critical),
            "affected_principals": principals,
            "via": [{"principal_id": a["principal_id"], "group": a["group_name"],
                     "scope": "org" if a["scope_type"] == "org" else f"project:{a['scope_id']}"}
                    for a in affected],
            "affected_grants": sorted(str(r[0]) for r in agents)}
