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
from typing import Any, Iterable, Optional

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
                  without_principal: Optional[str] = None,
                  without_members: Iterable[tuple[str, str]] = (),
                  permanent_only: bool = True) -> set[str]:
    """The active, non-network-bound people who hold Owner at org scope, directly or through a
    group, as if the named binding / group membership(s) / principal were gone.

    THE one definition of "an Owner remains" (§6.12), shared with the deferred DB trigger
    (schema.sql identity_assert_an_owner_remains): an Owner binding that EXPIRES does not count
    (`permanent_only`) — it would leave the org ownerless the moment it lapses, with nothing to
    refuse. `permanent_only=False` answers "who is an Owner right now" (expiring ones included,
    expired ones never)."""
    rows = db.execute(text("""
        SELECT p.id, b.id AS binding_id, m.group_id, b.expires_at
          FROM identity_role_bindings b
          LEFT JOIN identity_group_members m ON b.subject_type = 'group' AND m.group_id = b.subject_id
          JOIN identity_principals p
            ON p.id = CASE WHEN b.subject_type = 'principal' THEN b.subject_id ELSE m.principal_id END
         WHERE b.org_id = :o AND b.role_id = :owner AND b.scope_type = 'org'
           AND (b.expires_at IS NULL OR b.expires_at > now())
           AND p.kind = 'user' AND p.status = 'active' AND NOT p.network_bound"""),
        {"o": org_id, "owner": OWNER_ROLE_ID}).mappings().all()
    gone = set(without_members) | ({without_member} if without_member else set())
    out: set[str] = set()
    for r in rows:
        if permanent_only and r["expires_at"] is not None:
            continue
        if r["binding_id"] == without_binding or r["id"] == without_principal:
            continue
        if r["group_id"] is not None and (r["group_id"], r["id"]) in gone:
            continue
        out.add(r["id"])
    return out


def owner_ids(db: Any, org_id: str) -> set[str]:
    """Who is an Owner right now (expiring bindings included): for "an Admin cannot act on an
    Owner", never for "an Owner remains" (that is `active_owners`)."""
    return active_owners(db, org_id, permanent_only=False)


def owner_groups(db: Any, org_id: str) -> set[str]:
    """The groups that carry an org-scope Owner binding."""
    return {r[0] for r in db.execute(text(
        "SELECT subject_id FROM identity_role_bindings WHERE org_id = :o AND role_id = :owner AND "
        "scope_type = 'org' AND subject_type = 'group'"), {"o": org_id, "owner": OWNER_ROLE_ID})}


def guard_membership_removals(db: Any, org_id: str, removals: Iterable[tuple[str, str]]) -> None:
    """EVERY path that deletes group memberships (manual groups, SCIM replace/patch/delete, OIDC
    group sync, a person's deletion) calls this BEFORE deleting: when any removed (group, person)
    pair carries Owner reach, it takes the one Owner lock and refuses (409 last_owner) a change
    that would leave no permanent Owner. The deferred trigger on identity_group_members is the
    backstop for a path that forgets."""
    removals = [(str(g), str(p)) for g, p in removals]
    if not removals:
        return
    owning = owner_groups(db, org_id)
    if not any(g in owning for g, _p in removals):
        return
    lock_owners(db, org_id)
    if not active_owners(db, org_id, without_members=removals):
        raise AccessError(409, "last_owner", "Add another Owner first.")


LAST_OWNER_TRIGGER = "identity_last_owner"


def is_last_owner_violation(exc: BaseException) -> bool:
    """Did the database's deferred last-Owner trigger (schema.sql identity_assert_an_owner_remains)
    refuse this commit? Every path that can remove Owner reach — SCIM, people, groups, bindings,
    a cascade — lands here at COMMIT when its own pre-check missed a case; the API answers 409
    `last_owner` instead of a 500."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if LAST_OWNER_TRIGGER in str(exc):
            return True
        exc = getattr(exc, "orig", None) or exc.__cause__ or exc.__context__
    return False


def assert_owner_remains(db: Any, org_id: str, **without: Any) -> None:
    """409 when the change would leave the org with no active, permanent Owner. Callers hold
    lock_owners. S1 calls this (with without_principal=) before disabling or deleting a person.
    """
    if not active_owners(db, org_id, **without):
        raise AccessError(409, "last_owner", "Add another Owner first.")


def _target_scopes(db: Any, org_id: str, principal_id: str) -> list[str]:
    """The projects where a principal holds anything (directly or through a group)."""
    return sorted({r[0] for r in db.execute(text(
        "SELECT DISTINCT b.scope_id FROM identity_role_bindings b WHERE b.org_id = :o "
        "AND b.scope_type = 'project' AND ((b.subject_type = 'principal' AND b.subject_id = :p) "
        "OR (b.subject_type = 'group' AND b.subject_id IN (SELECT m.group_id FROM "
        "identity_group_members m WHERE m.principal_id = :p)))"), {"o": org_id, "p": principal_id})})


def uncovered(db: Any, ctx: AuthContext, target_principal_id: str) -> dict[str, list[str]]:
    """§6.11 for actions ON a person (credentials, MFA, email, reset links, sessions, status):
    the actor must hold a superset of what the target holds, at org scope and at every project
    the target holds anything in. Returns {scope label: permissions the actor lacks} — empty
    when the actor covers the target. An Admin therefore cannot take over an Owner, nor a
    custom-role holder with a permission the Admin lacks."""
    from .scope import ResolvedScope
    from .users import load_principal

    org = ctx.principal.org_id
    target = load_principal(db, target_principal_id)
    if target is None or target.org_id != org:
        return {}
    if target.status.value != "active":
        # a pending/disabled person's bindings still define what they would regain
        from dataclasses import replace
        from .principal import PrincipalStatus

        target = replace(target, status=PrincipalStatus.ACTIVE)
    scopes = [ResolvedScope.org_level(org)] + [ResolvedScope.in_project(org, p)
                                               for p in _target_scopes(db, org, target.id)]
    out: dict[str, list[str]] = {}
    for scope in scopes:
        theirs = authz.effective_permissions(target, scope, db=db).permissions
        if not theirs:
            continue
        mine = authz.effective_permissions(ctx, scope, db=db).permissions
        missing = sorted(set(theirs) - set(mine))
        if missing:
            out[scope.label()] = missing
    return out


def require_covers(db: Any, ctx: AuthContext, target_principal_id: str, permission: str) -> None:
    """403 unless the actor covers the target (see `uncovered`). Self-actions are the caller's
    business (they already pass their own route's permission)."""
    if target_principal_id == ctx.principal.id:
        return
    missing = uncovered(db, ctx, target_principal_id)
    if missing:
        if target_principal_id in owner_ids(db, ctx.principal.org_id):
            permission = "owner:assign"         # acting on an Owner takes another Owner
        raise AccessError(403, "forbidden", "They hold access you don't.",
                          {"reason": "target_exceeds_actor", "permission": permission,
                           "missing": {k: v[:20] for k, v in missing.items()}})


def _present(row: Any) -> dict:
    via_group = None
    if row["subject_type"] == "group":
        via_group = {"id": row["subject_id"], "name": row.get("group_name"),
                     # where the group's membership comes from: manual | scim | oidc
                     "provider": row.get("group_source")}
    return {"id": row["id"], "subject_type": row["subject_type"], "subject_id": row["subject_id"],
            "via_group": via_group,
            "role_id": row["role_id"], "role_key": row.get("role_key"),
            "scope": "org" if row["scope_type"] == "org" else f"project:{row['scope_id']}",
            "scope_type": row["scope_type"], "scope_id": row["scope_id"],
            "granted_by": row["granted_by"], "granted_at": row["granted_at"],
            "expires_at": row["expires_at"], "reason": row["reason"]}


_SELECT = ("SELECT b.*, r.key AS role_key, g.name AS group_name, g.source AS group_source "
           "FROM identity_role_bindings b "
           "JOIN identity_access_roles r ON r.id = b.role_id "
           "LEFT JOIN identity_groups g ON b.subject_type = 'group' AND g.id = b.subject_id ")


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


def refuse_self_raise(db: Any, ctx: AuthContext, permissions: Iterable[str], scope: Any) -> None:
    """A33 through a group (M3): a change that lands on the CALLER (a binding on a group they
    are in, adding themselves to a group that carries bindings) may not raise their own access.
    The grant ceiling already says they hold every permission at that scope; this also refuses
    turning what they hold only for a while (an expiring or break-glass binding) into something
    permanent. Comparing against the caller's PERMANENT bindings, narrowed by its credential."""
    wanted = {k for k in permissions
              if not (scope.kind.value == "project" and authz.PERMISSION_META[k].org_only)}
    permanent = set(authz.permanent_permissions(ctx.principal.org_id, ctx.principal.id, scope, db=db))
    if ctx.credential is not None:
        permanent &= set(ctx.credential.scopes)
    raised = sorted(wanted - permanent)
    if raised:
        raise AccessError(403, "forbidden", "Nobody raises their own access.",
                          {"reason": "self_binding", "missing": raised[:20],
                           "scope": scope.label()})


def _caller_in_group(db: Any, ctx: AuthContext, group_id: str) -> bool:
    return db.execute(text("SELECT 1 FROM identity_group_members WHERE group_id = :g AND "
                           "principal_id = :p"), {"g": group_id, "p": ctx.principal.id}).first() is not None


def _grant_rules(db: Any, ctx: AuthContext, role: dict, scope_type: str, scope_id: str,
                 subject_type: str, subject_id: str, *, granting: bool = True) -> None:
    org = ctx.principal.org_id
    scope = scope_of(org, scope_type, scope_id)
    if subject_type == "principal" and subject_id == ctx.principal.id:
        raise AccessError(403, "forbidden", "Nobody edits their own access roles.",
                          {"reason": "self_binding"})                                 # A33
    require(ctx, "role_binding:assign", scope, db=db)
    if role["id"] == OWNER_ROLE_ID:
        require(ctx, "owner:assign", scope_of(org, "org", org), db=db)              # A31
    perms = permissions_of(db, role["id"])
    require_ceiling(ctx, perms, scope, db=db)
    if granting and subject_type == "group" and _caller_in_group(db, ctx, subject_id):
        refuse_self_raise(db, ctx, perms, scope)                                    # A33 via a group


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
    from . import signin as _signin

    affected = _people_of(db, subject_type, subject_id)
    before = _signin.mfa_required_now(db, affected)
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
    _signin.grant_first_factor_on_promotion(db, affected, was_required=before)
    return get_binding(db, org, binding_id)


def _people_of(db: Any, subject_type: str, subject_id: str) -> list[str]:
    """The user principals a binding on this subject reaches (the person, or a group's people)."""
    if subject_type == "principal":
        return [subject_id]
    return [r[0] for r in db.execute(text(
        "SELECT m.principal_id FROM identity_group_members m JOIN identity_principals p ON "
        "p.id = m.principal_id WHERE m.group_id = :g AND p.kind = 'user'"), {"g": subject_id}).all()]


def remove_binding(db: Any, ctx: AuthContext, binding_id: str, *,
                   expect_project: Optional[str] = None) -> None:
    org = ctx.principal.org_id
    binding = get_binding(db, org, binding_id)
    if expect_project is not None and (binding["scope_type"] != "project"
                                       or binding["scope_id"] != expect_project):
        raise AccessError(404, "not_found")   # a project admin cannot reach another scope's binding
    role = {"id": binding["role_id"], "key": binding["role_key"]}
    _grant_rules(db, ctx, role, binding["scope_type"], binding["scope_id"],
                 binding["subject_type"], binding["subject_id"], granting=False)
    if binding["role_id"] == OWNER_ROLE_ID:
        lock_owners(db, org)
        assert_owner_remains(db, org, without_binding=binding_id)                  # A38
    db.execute(text("DELETE FROM identity_role_bindings WHERE id = :id AND org_id = :o"),
               {"id": binding_id, "o": org})
    audit_event(ctx, "owner.removed" if binding["role_id"] == OWNER_ROLE_ID else "role_binding.removed",
                db=db, target_type=binding["subject_type"], target_id=binding["subject_id"],
                scope=binding["scope"], details={"role": binding["role_key"], "binding_id": binding_id})
    authz.bump_epoch(org, db=db, reason="role_binding.removed")


def reassign_role(db: Any, ctx: AuthContext, from_role_id: str, to_role_id: str, *,
                  delete: bool = False) -> dict:
    """§6.10 "Reassign 4 people to… and delete": move EVERY binding of a role (people and groups,
    every scope) to another role, each move under the full §6.11 rules (the caller covers both
    roles at that scope, nobody moves their own, Owner needs owner:assign, the target role must
    bind at that scope), then optionally delete the emptied role.

    Every rule is checked BEFORE anything is written (a refusal changes nothing), then the moves
    are written and the epoch moves once — the PDP is never asked mid-way through its own change."""
    from . import roles as roles_service

    org = ctx.principal.org_id
    source = roles_service._role_row(db, org, from_role_id)          # noqa: SLF001 — same service
    target = roles_service._role_row(db, org, to_role_id)            # noqa: SLF001
    if source is None or source["system"] or target is None or target["system"]:
        raise AccessError(404, "not_found")
    if from_role_id == to_role_id:
        raise AccessError(422, "same_role", "Pick a different role to move them to.")
    if delete:
        roles_service._editable(db, org, from_role_id)              # noqa: SLF001 — 409 for a built-in
        require_ceiling(ctx, permissions_of(db, from_role_id), roles_service.org_scope(ctx), db=db)
    rows = list_bindings(db, org, role_id=from_role_id)
    plan = []
    for b in rows:                                                  # 1. every check, no writes
        if b["scope_type"] not in target["bindable_scopes"]:
            raise AccessError(422, "scope_not_bindable",
                              f"{target['key']} binds at {', '.join(target['bindable_scopes'])} only.",
                              {"binding_id": b["id"]})
        _grant_rules(db, ctx, {"id": from_role_id, "key": source["key"]}, b["scope_type"], b["scope_id"],
                     b["subject_type"], b["subject_id"], granting=False)
        _grant_rules(db, ctx, dict(target), b["scope_type"], b["scope_id"], b["subject_type"],
                     b["subject_id"])
        already = db.execute(text(
            "SELECT id FROM identity_role_bindings WHERE subject_type = :st AND subject_id = :s AND "
            "role_id = :r AND scope_type = :t AND scope_id = :i"),
            {"st": b["subject_type"], "s": b["subject_id"], "r": to_role_id, "t": b["scope_type"],
             "i": b["scope_id"]}).scalar()
        plan.append((b, already))
    moved: list[str] = []
    for b, already in plan:                                         # 2. the writes
        new_id_ = already
        if already is None:
            new_id_ = new_id("rbd")
            db.execute(text(
                "INSERT INTO identity_role_bindings (id, org_id, subject_type, subject_id, role_id, "
                "scope_type, scope_id, granted_by, expires_at, reason) VALUES "
                "(:id, :o, :st, :s, :r, :t, :i, :by, :e, :why)"),
                {"id": new_id_, "o": org, "st": b["subject_type"], "s": b["subject_id"], "r": to_role_id,
                 "t": b["scope_type"], "i": b["scope_id"], "by": ctx.principal.id, "e": b["expires_at"],
                 "why": f"reassigned from {source['key']}"})
            audit_event(ctx, "owner.added" if to_role_id == OWNER_ROLE_ID else "role_binding.added", db=db,
                        target_type=b["subject_type"], target_id=b["subject_id"], scope=b["scope"],
                        details={"role": target["key"], "binding_id": new_id_, "reassigned_from": source["key"]})
        db.execute(text("DELETE FROM identity_role_bindings WHERE id = :id AND org_id = :o"),
                   {"id": b["id"], "o": org})
        audit_event(ctx, "owner.removed" if from_role_id == OWNER_ROLE_ID else "role_binding.removed", db=db,
                    target_type=b["subject_type"], target_id=b["subject_id"], scope=b["scope"],
                    details={"role": source["key"], "binding_id": b["id"], "reassigned_to": target["key"]})
        moved.append(new_id_)
    if from_role_id == OWNER_ROLE_ID:
        lock_owners(db, org)
        assert_owner_remains(db, org)
    deleted = False
    if delete:
        db.execute(text("DELETE FROM identity_access_roles WHERE id = :r"), {"r": from_role_id})
        audit_event(ctx, "access_role.deleted", db=db, target_type="access_role", target_id=from_role_id,
                    details={"key": source["key"]})
        deleted = True
    authz.bump_epoch(org, db=db, reason="access_role.reassigned")
    return {"moved": len(moved), "bindings": [get_binding(db, org, b) for b in moved], "deleted": deleted}


def grantors(db: Any, org_id: str, permission: str, *, project_id: Optional[str] = None,
             limit: int = 10) -> list[dict]:
    """Who could grant `permission` (at the project, or org-wide): active people who hold
    role_binding:assign AND the permission itself there (the grant ceiling). Names only — this
    feeds "ask Priya or Sam" on an Access-denied page, never anyone's security detail."""
    from .principal import Principal, PrincipalKind, PrincipalStatus
    from .scope import ResolvedScope

    scope = (ResolvedScope.in_project(org_id, project_id) if project_id
             else ResolvedScope.org_level(org_id))
    rows = db.execute(text(
        "SELECT DISTINCT p.id, p.display_name FROM identity_principals p "
        "WHERE p.org_id = :o AND p.kind = 'user' AND p.status = 'active' AND NOT p.network_bound "
        "AND (EXISTS (SELECT 1 FROM identity_role_bindings b WHERE b.org_id = :o "
        "             AND b.subject_type = 'principal' AND b.subject_id = p.id) "
        "  OR EXISTS (SELECT 1 FROM identity_group_members m JOIN identity_role_bindings b "
        "             ON b.subject_type = 'group' AND b.subject_id = m.group_id "
        "             WHERE m.principal_id = p.id AND b.org_id = :o)) "
        "ORDER BY p.display_name"), {"o": org_id}).mappings().all()
    out: list[dict] = []
    for r in rows:
        subject = Principal(r["id"], PrincipalKind.USER, org_id, PrincipalStatus.ACTIVE, r["display_name"])
        held = authz.effective_permissions(subject, scope, db=db).permissions
        if "role_binding:assign" in held and permission in held:
            out.append({"principal_id": r["id"], "display_name": r["display_name"]})
            if len(out) >= limit:
                break
    return out
