"""Groups: flat, manual or IdP-sourced (SCIM / OIDC, read-only here). Stream S2. IDENTITY.md §6.8.

Changing a group's membership IS granting: adding someone needs group:manage_members (checked by
the route) AND the caller holding every permission the group's role bindings carry, at each
binding's scope (A34). Removing a member who holds Owner only through the group takes the Owner
lock and refuses to leave no Owner, because the DB trigger watches bindings and principals, not
group membership.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from sqlalchemy import text

from . import authz
from .bindings import (OWNER_ROLE_ID, assert_owner_remains, guard_membership_removals, lock_owners,
                       refuse_self_raise)
from .ids import new_id
from .principal import AuthContext
from .roles import AccessError, audit_event, permissions_of, require, require_ceiling, scope_of

GROUP_KEY = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")


def _row(db: Any, org_id: str, group_id: str) -> dict:
    row = db.execute(text("SELECT id, key, name, description, source, external_id, created_at "
                          "FROM identity_groups WHERE id = :g AND org_id = :o"),
                     {"g": group_id, "o": org_id}).mappings().first()
    if row is None:
        raise AccessError(404, "not_found")
    return dict(row)


def _manual(row: dict) -> None:
    if row["source"] != "manual":
        raise AccessError(409, "idp_group_read_only",
                          f"This group comes from {row['source'].upper()}; change it there.")


def members(db: Any, group_id: str) -> list[dict]:
    rows = db.execute(text(
        "SELECT m.principal_id, p.display_name, p.kind, p.status, m.source, m.added_by, m.added_at "
        "FROM identity_group_members m JOIN identity_principals p ON p.id = m.principal_id "
        "WHERE m.group_id = :g ORDER BY p.display_name"), {"g": group_id}).mappings().all()
    return [dict(r) for r in rows]


def get_group(db: Any, org_id: str, group_id: str) -> dict:
    row = _row(db, org_id, group_id)
    return {**row, "members": members(db, group_id)}


def list_groups(db: Any, org_id: str) -> list[dict]:
    rows = db.execute(text(
        "SELECT g.id, g.key, g.name, g.description, g.source, g.created_at, "
        "(SELECT count(*) FROM identity_group_members m WHERE m.group_id = g.id) AS member_count "
        "FROM identity_groups g WHERE g.org_id = :o ORDER BY g.name"), {"o": org_id}).mappings().all()
    return [dict(r) for r in rows]


def _binding_ceilings(db: Any, ctx: AuthContext, group_id: str) -> list[dict]:
    """Every binding of the group, after checking the caller holds what each one grants."""
    org = ctx.principal.org_id
    rows = db.execute(text("SELECT id, role_id, scope_type, scope_id FROM identity_role_bindings "
                           "WHERE org_id = :o AND subject_type = 'group' AND subject_id = :g"),
                      {"o": org, "g": group_id}).mappings().all()
    for b in rows:
        require_ceiling(ctx, permissions_of(db, b["role_id"]), scope_of(org, b["scope_type"], b["scope_id"]),
                        db=db)
        if b["role_id"] == OWNER_ROLE_ID:
            require(ctx, "owner:assign", scope_of(org, "org", org), db=db)          # A31
    return [dict(b) for b in rows]


def membership_ceilings(db: Any, ctx: AuthContext, group_ids: Any, *, for_self: bool = False) -> None:
    """Putting anyone into these groups grants what their bindings carry: the caller must hold it
    all (A34), and the groups must be this org's MANUAL groups. Every path that adds a member
    runs this: add_member, and an invitation at create AND at accept (C1)."""
    org = ctx.principal.org_id
    for gid in sorted(set(group_ids or ())):
        _manual(_row(db, org, gid))
        for b in _binding_ceilings(db, ctx, gid):
            if for_self:
                refuse_self_raise(db, ctx, permissions_of(db, b["role_id"]),
                                  scope_of(org, b["scope_type"], b["scope_id"]))


def create_group(db: Any, ctx: AuthContext, *, key: str, name: str, description: str = "") -> dict:
    org = ctx.principal.org_id
    if not GROUP_KEY.match(key or "") or not (name or "").strip():
        raise AccessError(422, "invalid_group")
    if db.execute(text("SELECT 1 FROM identity_groups WHERE org_id = :o AND key = :k"),
                  {"o": org, "k": key}).first():
        raise AccessError(409, "group_exists")
    group_id = new_id("grp")
    db.execute(text("INSERT INTO identity_groups (id, org_id, key, name, description, source) "
                    "VALUES (:id, :o, :k, :n, :d, 'manual')"),
               {"id": group_id, "o": org, "k": key, "n": name.strip(), "d": description or None})
    audit_event(ctx, "group.created", db=db, target_type="group", target_id=group_id,
                details={"key": key})
    return get_group(db, org, group_id)


def update_group(db: Any, ctx: AuthContext, group_id: str, *, name: Optional[str] = None,
                 description: Optional[str] = None) -> dict:
    org = ctx.principal.org_id
    row = _row(db, org, group_id)
    _manual(row)
    db.execute(text("UPDATE identity_groups SET name = :n, description = :d WHERE id = :g"),
               {"n": (name or row["name"]).strip(),
                "d": row["description"] if description is None else description, "g": group_id})
    audit_event(ctx, "group.updated", db=db, target_type="group", target_id=group_id)
    return get_group(db, org, group_id)


def delete_group(db: Any, ctx: AuthContext, group_id: str) -> None:
    """Deleting a group removes its bindings: the caller must hold what they grant, and an Owner
    reach through it must not be the last one."""
    org = ctx.principal.org_id
    row = _row(db, org, group_id)
    _manual(row)
    bindings = _binding_ceilings(db, ctx, group_id)
    if any(b["role_id"] == OWNER_ROLE_ID for b in bindings):
        lock_owners(db, org)
        for b in bindings:
            if b["role_id"] == OWNER_ROLE_ID:
                assert_owner_remains(db, org, without_binding=b["id"])
    db.execute(text("DELETE FROM identity_role_bindings WHERE org_id = :o AND subject_type = 'group' "
                    "AND subject_id = :g"), {"o": org, "g": group_id})
    db.execute(text("DELETE FROM identity_group_members WHERE group_id = :g"), {"g": group_id})
    db.execute(text("DELETE FROM identity_groups WHERE id = :g AND org_id = :o"), {"g": group_id, "o": org})
    audit_event(ctx, "group.deleted", db=db, target_type="group", target_id=group_id,
                details={"key": row["key"], "bindings_removed": len(bindings)})
    authz.bump_epoch(org, db=db, reason="group.deleted")


def add_member(db: Any, ctx: AuthContext, group_id: str, principal_id: str) -> dict:
    org = ctx.principal.org_id
    row = _row(db, org, group_id)
    _manual(row)
    kind = db.execute(text("SELECT kind FROM identity_principals WHERE id = :p AND org_id = :o"),
                      {"p": principal_id, "o": org}).scalar()
    if kind is None:
        raise AccessError(404, "not_found")
    if kind == "system":
        raise AccessError(422, "system_principal")
    membership_ceilings(db, ctx, [group_id], for_self=principal_id == ctx.principal.id)  # A34, A33
    inserted = db.execute(text(
        "INSERT INTO identity_group_members (group_id, principal_id, source, added_by) "
        "VALUES (:g, :p, 'manual', :b) ON CONFLICT DO NOTHING"),
        {"g": group_id, "p": principal_id, "b": ctx.principal.id}).rowcount
    if inserted:
        audit_event(ctx, "group.member_added", db=db, target_type="principal",
                    target_id=principal_id, details={"group_id": group_id})
        authz.bump_epoch(org, db=db, reason="group.member_added")
    return get_group(db, org, group_id)


def remove_member(db: Any, ctx: AuthContext, group_id: str, principal_id: str) -> dict:
    org = ctx.principal.org_id
    row = _row(db, org, group_id)
    _manual(row)
    present = db.execute(text("SELECT 1 FROM identity_group_members WHERE group_id = :g "
                              "AND principal_id = :p"), {"g": group_id, "p": principal_id}).first()
    if present is None:
        raise AccessError(404, "not_found")
    _binding_ceilings(db, ctx, group_id)
    guard_membership_removals(db, org, [(group_id, principal_id)])
    db.execute(text("DELETE FROM identity_group_members WHERE group_id = :g AND principal_id = :p"),
               {"g": group_id, "p": principal_id})
    audit_event(ctx, "group.member_removed", db=db, target_type="principal", target_id=principal_id,
                details={"group_id": group_id})
    authz.bump_epoch(org, db=db, reason="group.member_removed")
    return get_group(db, org, group_id)
