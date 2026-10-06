"""Role bindings: (subject, access role, scope). Stream S2. IDENTITY.md §6.1, §6.7, §6.11, §6.12.

Granting is the dangerous operation, so every rule of §6.11 runs here, in the caller's transaction:
  - the caller holds role_binding:assign AT THE BINDING'S SCOPE (not only the route's);
  - an Owner binding also needs owner:assign (A31);
  - nobody edits their own bindings (A33);
  - the caller holds every permission the role carries at that scope (the grant ceiling);
  - removing an Owner reach takes the per-org advisory lock and refuses to leave no active,
    non-network-bound Owner (A38); the deferred DB trigger is the backstop.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import text

from . import authz
from .ids import new_id
from .principal import AuthContext
from .roles import AccessError, audit_event, permissions_of, require, require_ceiling, scope_of

OWNER_ROLE_ID = "arl_owner"
OWNER_LOCK = "identity_owner:{org}"


def lock_owners(db: Any, org_id: str) -> None:
    """Serialise every change that can remove Owner reach in this org (§6.12)."""
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": OWNER_LOCK.format(org=org_id)})


def active_owners(db: Any, org_id: str, *, without_binding: Optional[str] = None,
                  without_member: Optional[tuple[str, str]] = None,
                  without_principal: Optional[str] = None) -> set[str]:
    """The active, non-network-bound people who hold Owner at org scope, directly or through a
    group, as if the named binding / group membership / principal were gone."""
    rows = db.execute(text("""
        SELECT p.id, b.id AS binding_id, m.group_id
          FROM identity_role_bindings b
          LEFT JOIN identity_group_members m ON b.subject_type = 'group' AND m.group_id = b.subject_id
          JOIN identity_principals p
            ON p.id = CASE WHEN b.subject_type = 'principal' THEN b.subject_id ELSE m.principal_id END
         WHERE b.org_id = :o AND b.role_id = :owner AND b.scope_type = 'org'
           AND (b.expires_at IS NULL OR b.expires_at > now())
           AND p.kind = 'user' AND p.status = 'active' AND NOT p.network_bound"""),
        {"o": org_id, "owner": OWNER_ROLE_ID}).mappings().all()
    out: set[str] = set()
    for r in rows:
        if r["binding_id"] == without_binding or r["id"] == without_principal:
            continue
        if without_member and (r["group_id"], r["id"]) == without_member:
            continue
        out.add(r["id"])
    return out


def assert_owner_remains(db: Any, org_id: str, **without: Any) -> None:
    """409 when the change would leave the org with no active Owner. Callers hold lock_owners.
    S1 calls this (with without_principal=) before disabling or deleting a person."""
    if not active_owners(db, org_id, **without):
        raise AccessError(409, "last_owner", "Add another Owner first.")


def _present(row: Any) -> dict:
    return {"id": row["id"], "subject_type": row["subject_type"], "subject_id": row["subject_id"],
            "role_id": row["role_id"], "role_key": row.get("role_key"),
            "scope": "org" if row["scope_type"] == "org" else f"project:{row['scope_id']}",
            "scope_type": row["scope_type"], "scope_id": row["scope_id"],
            "granted_by": row["granted_by"], "granted_at": row["granted_at"],
            "expires_at": row["expires_at"], "reason": row["reason"]}


_SELECT = ("SELECT b.*, r.key AS role_key FROM identity_role_bindings b "
           "JOIN identity_access_roles r ON r.id = b.role_id ")


def list_bindings(db: Any, org_id: str, *, subject_id: Optional[str] = None,
                  project_id: Optional[str] = None, role_id: Optional[str] = None) -> list[dict]:
    sql = _SELECT + "WHERE b.org_id = :o"
    params: dict[str, Any] = {"o": org_id}
    if subject_id:
        sql += " AND b.subject_id = :s"
        params["s"] = subject_id
    if project_id:
        sql += " AND b.scope_type = 'project' AND b.scope_id = :p"
        params["p"] = project_id
    if role_id:
        sql += " AND b.role_id = :r"
        params["r"] = role_id
    return [_present(r) for r in db.execute(text(sql + " ORDER BY b.granted_at"), params).mappings().all()]


def get_binding(db: Any, org_id: str, binding_id: str) -> dict:
    row = db.execute(text(_SELECT + "WHERE b.id = :id AND b.org_id = :o"),
                     {"id": binding_id, "o": org_id}).mappings().first()
    if row is None:
        raise AccessError(404, "not_found")
    return _present(row)


def _subject_exists(db: Any, org_id: str, subject_type: str, subject_id: str) -> None:
    if subject_type == "principal":
        row = db.execute(text("SELECT kind FROM identity_principals WHERE id = :s AND org_id = :o"),
                         {"s": subject_id, "o": org_id}).first()
        if row is None:
            raise AccessError(404, "not_found")
        if row[0] == "system":
            raise AccessError(422, "system_principal", "System principals hold fixed sets.")
    elif subject_type == "group":
        if db.execute(text("SELECT 1 FROM identity_groups WHERE id = :s AND org_id = :o"),
                      {"s": subject_id, "o": org_id}).first() is None:
            raise AccessError(404, "not_found")
    else:
        raise AccessError(422, "invalid_subject_type")


def _project_exists(db: Any, org_id: str, project_id: str) -> None:
    found = db.execute(text("SELECT 1 FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                            "WHERE p.id = :p AND w.org_id = :o"), {"p": project_id, "o": org_id}).first()
    if found is None:
        raise AccessError(404, "not_found")


def _grant_rules(db: Any, ctx: AuthContext, role: dict, scope_type: str, scope_id: str,
                 subject_type: str, subject_id: str) -> None:
    org = ctx.principal.org_id
    scope = scope_of(org, scope_type, scope_id)
    if subject_type == "principal" and subject_id == ctx.principal.id:
        raise AccessError(403, "forbidden", "Nobody edits their own access roles.",
                          {"reason": "self_binding"})                                 # A33
    require(ctx, "role_binding:assign", scope, db=db)
    if role["id"] == OWNER_ROLE_ID:
        require(ctx, "owner:assign", scope_of(org, "org", org), db=db)              # A31
    require_ceiling(ctx, permissions_of(db, role["id"]), scope, db=db)


def add_binding(db: Any, ctx: AuthContext, *, subject_type: str, subject_id: str, role_id: str,
                scope_type: str, scope_id: Optional[str] = None,
                expires_at: Optional[datetime] = None, reason: Optional[str] = None) -> dict:
    org = ctx.principal.org_id
    role = db.execute(text("SELECT id, key, system, bindable_scopes FROM identity_access_roles "
                           "WHERE id = :r AND (org_id IS NULL OR org_id = :o)"),
                      {"r": role_id, "o": org}).mappings().first()
    if role is None or role["system"]:
        raise AccessError(404, "not_found")
    if scope_type not in ("org", "project"):
        raise AccessError(422, "invalid_scope")
    if scope_type not in role["bindable_scopes"]:
        raise AccessError(422, "scope_not_bindable",
                          f"{role['key']} binds at {', '.join(role['bindable_scopes'])} only.")
    if scope_type == "org":
        scope_id = org
    else:
        if not scope_id:
            raise AccessError(422, "invalid_scope")
        _project_exists(db, org, scope_id)
    _subject_exists(db, org, subject_type, subject_id)
    _grant_rules(db, ctx, dict(role), scope_type, scope_id, subject_type, subject_id)
    exists = db.execute(text(
        "SELECT 1 FROM identity_role_bindings WHERE subject_type = :st AND subject_id = :s "
        "AND role_id = :r AND scope_type = :t AND scope_id = :i"),
        {"st": subject_type, "s": subject_id, "r": role_id, "t": scope_type, "i": scope_id}).first()
    if exists:
        raise AccessError(409, "binding_exists")
    binding_id = new_id("rbd")
    db.execute(text(
        "INSERT INTO identity_role_bindings (id, org_id, subject_type, subject_id, role_id, "
        "scope_type, scope_id, granted_by, expires_at, reason) VALUES "
        "(:id, :o, :st, :s, :r, :t, :i, :b, :e, :why)"),
        {"id": binding_id, "o": org, "st": subject_type, "s": subject_id, "r": role_id,
         "t": scope_type, "i": scope_id, "b": ctx.principal.id, "e": expires_at, "why": reason})
    label = "org" if scope_type == "org" else f"project:{scope_id}"
    audit_event(ctx, "owner.added" if role_id == OWNER_ROLE_ID else "role_binding.added", db=db,
                target_type=subject_type, target_id=subject_id, scope=label,
                details={"role": role["key"], "binding_id": binding_id})
    authz.bump_epoch(org, db=db, reason="role_binding.added")
    return get_binding(db, org, binding_id)


def remove_binding(db: Any, ctx: AuthContext, binding_id: str, *,
                   expect_project: Optional[str] = None) -> None:
    org = ctx.principal.org_id
    binding = get_binding(db, org, binding_id)
    if expect_project is not None and (binding["scope_type"] != "project"
                                       or binding["scope_id"] != expect_project):
        raise AccessError(404, "not_found")   # a project admin cannot reach another scope's binding
    role = {"id": binding["role_id"], "key": binding["role_key"]}
    _grant_rules(db, ctx, role, binding["scope_type"], binding["scope_id"],
                 binding["subject_type"], binding["subject_id"])
    if binding["role_id"] == OWNER_ROLE_ID:
        lock_owners(db, org)
        assert_owner_remains(db, org, without_binding=binding_id)                  # A38
    db.execute(text("DELETE FROM identity_role_bindings WHERE id = :id AND org_id = :o"),
               {"id": binding_id, "o": org})
    audit_event(ctx, "owner.removed" if binding["role_id"] == OWNER_ROLE_ID else "role_binding.removed",
                db=db, target_type=binding["subject_type"], target_id=binding["subject_id"],
                scope=binding["scope"], details={"role": binding["role_key"], "binding_id": binding_id})
    authz.bump_epoch(org, db=db, reason="role_binding.removed")
