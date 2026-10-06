"""SCIM 2.0 provisioning at org level (IDENTITY.md §4.9; RFC 7643/7644 subset). Stream S1.

  - Callers hold a `boss_scim_` key of a `sys_scim_<provider>` principal; that key can only use
    `scim:provision` (the PDP, plus a defence-in-depth check in the router).
  - People created over SCIM are `pending` (no password) until their first SSO sign-in.
  - `active=false` disables the person with the full cascade: sessions revoked, grants and run
    tokens revoked (S3's revoke_cascade). `active=true` re-enables (active again only for someone
    who has signed in before; otherwise back to pending).
  - Groups land read-only in BOSS (`source=scim`). SCIM can NEVER assign access roles: a request
    carrying `roles`/`entitlements` is refused (400), never silently ignored.
  - Nothing is ever hard-deleted: DELETE of a user disables; DELETE of a group removes the group,
    its memberships and the role bindings that pointed at it (a binding to nothing grants nothing).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from sqlalchemy import text

from . import ids, sessions, users
from .audit import Outcome
from .authn_support import bump_epoch, emit
from .principal import AuthContext

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
FORBIDDEN_ATTRIBUTES = frozenset({"roles", "entitlements", "x509certificates", "password"})
MAX_COUNT = 200


class ScimError(Exception):
    def __init__(self, status: int, detail: str, scim_type: Optional[str] = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.scim_type = scim_type

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"schemas": [ERROR_SCHEMA], "status": str(self.status), "detail": self.detail}
        if self.scim_type:
            out["scimType"] = self.scim_type
        return out


def _refuse_role_attributes(payload: dict[str, Any]) -> None:
    for key in payload:
        if key.lower() in FORBIDDEN_ATTRIBUTES:
            raise ScimError(400, f"SCIM cannot set {key}: access roles are bound by admins in BOSS",
                            "invalidValue")


def _email_of(payload: dict[str, Any]) -> str:
    emails = payload.get("emails") or []
    if isinstance(emails, list):
        primary = [e for e in emails if isinstance(e, dict) and e.get("primary") and e.get("value")]
        anyone = [e for e in emails if isinstance(e, dict) and e.get("value")]
        chosen = (primary or anyone)
        if chosen:
            return str(chosen[0]["value"]).strip()
    return str(payload.get("userName") or "").strip()


def _display_name(payload: dict[str, Any], fallback: str) -> str:
    if payload.get("displayName"):
        return str(payload["displayName"])[:200]
    name = payload.get("name") or {}
    if isinstance(name, dict):
        if name.get("formatted"):
            return str(name["formatted"])[:200]
        joined = " ".join(str(name[k]) for k in ("givenName", "familyName") if name.get(k))
        if joined:
            return joined[:200]
    return fallback


# ---- users ---------------------------------------------------------------------------------------

def _user_row(db: Any, org_id: str, user_id: str) -> Optional[users.UserRow]:
    user = users.by_id(db, user_id, org_id=org_id)
    if user is None or user.network_bound:
        return None
    return user


def user_resource(user: users.UserRow, *, base: str = "") -> dict[str, Any]:
    return {
        "schemas": [USER_SCHEMA], "id": user.principal_id, "userName": user.email,
        "displayName": user.display_name, "name": {"formatted": user.display_name},
        "emails": [{"value": user.email, "primary": True}],
        "active": user.status in ("active", "pending"),
        "meta": {"resourceType": "User", "location": f"{base}/scim/v2/Users/{user.principal_id}"},
    }


def list_users(db: Any, ctx: AuthContext, *, filter_: Optional[str], start: int, count: int
               ) -> dict[str, Any]:
    org = ctx.principal.org_id
    sql = ("SELECT p.id FROM identity_principals p JOIN identity_users u ON u.principal_id = p.id "
           "WHERE p.org_id = :o AND p.kind = 'user' AND NOT p.network_bound")
    params: dict[str, Any] = {"o": org}
    if filter_:
        attr, value = _parse_eq_filter(filter_, {"username", "emails.value", "emails"})
        sql += " AND lower(u.email) = lower(:v)"
        params["v"] = value
        del attr
    ids_ = [r[0] for r in db.execute(text(sql + " ORDER BY p.created_at, p.id"), params)]
    start = max(1, start)
    count = max(0, min(count, MAX_COUNT))
    page = ids_[start - 1:start - 1 + count]
    resources = [user_resource(u) for u in (users.by_id(db, i) for i in page) if u]
    return {"schemas": [LIST_SCHEMA], "totalResults": len(ids_), "startIndex": start,
            "itemsPerPage": len(resources), "Resources": resources}


def _parse_eq_filter(filter_: str, allowed: set[str]) -> tuple[str, str]:
    parts = filter_.strip().split(None, 2)
    if len(parts) != 3 or parts[1].lower() != "eq" or parts[0].lower() not in allowed:
        raise ScimError(400, "only `<attribute> eq \"value\"` filters are supported", "invalidFilter")
    value = parts[2].strip()
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        raise ScimError(400, "filter value must be quoted", "invalidFilter")
    return parts[0].lower(), value[1:-1]


def create_user(db: Any, ctx: AuthContext, payload: dict[str, Any]) -> dict[str, Any]:
    _refuse_role_attributes(payload)
    email = _email_of(payload)
    if "@" not in email or len(email) > 320:
        raise ScimError(400, "userName or a primary email address is required", "invalidValue")
    if users.by_email(db, email):
        raise ScimError(409, "a person with this email already exists", "uniqueness")
    pid = users.create_user(db, org_id=ctx.principal.org_id, email=email,
                            display_name=_display_name(payload, email), password_hash=None,
                            status="pending", created_by=ctx.principal.id)
    emit(db, org_id=ctx.principal.org_id, event_type="scim.user.created", outcome=Outcome.SUCCESS, ctx=ctx,
         target_type="user", target_id=pid)
    if payload.get("active") is False:
        _set_active(db, ctx, users.by_id(db, pid), False)
    return user_resource(users.by_id(db, pid))


def get_user(db: Any, ctx: AuthContext, user_id: str) -> dict[str, Any]:
    user = _user_row(db, ctx.principal.org_id, user_id)
    if user is None:
        raise ScimError(404, "not found")
    return user_resource(user)


def _set_active(db: Any, ctx: AuthContext, user: Optional[users.UserRow], active: bool) -> None:
    if user is None:
        return
    org = ctx.principal.org_id
    if not active:
        if user.status == "disabled":
            return
        users.set_status(db, user.principal_id, "disabled", by=ctx.principal.id, reason="scim active=false")
        ended = sessions.revoke_all(db, user.principal_id, by=ctx.principal.id, reason="disabled by SCIM")
        db.execute(text("UPDATE identity_api_credentials SET suspended_at = now() WHERE principal_id = :p "
                        "AND suspended_at IS NULL AND revoked_at IS NULL"), {"p": user.principal_id})
        _cascade(db, ctx, user.principal_id)
        emit(db, org_id=org, event_type="scim.user.deactivated", outcome=Outcome.SUCCESS, ctx=ctx,
             target_type="user", target_id=user.principal_id, details={"sessions_revoked": ended})
        emit(db, org_id=org, event_type="user.disabled", outcome=Outcome.SUCCESS, ctx=ctx,
             target_type="user", target_id=user.principal_id, details={"via": "scim"})
    else:
        if user.status != "disabled":
            return
        new_status = "active" if user.last_login_at else "pending"
        users.set_status(db, user.principal_id, new_status, by=ctx.principal.id, reason="scim active=true")
        db.execute(text("UPDATE identity_api_credentials SET suspended_at = NULL WHERE principal_id = :p "
                        "AND revoked_at IS NULL"), {"p": user.principal_id})
        emit(db, org_id=org, event_type="user.enabled", outcome=Outcome.SUCCESS, ctx=ctx,
             target_type="user", target_id=user.principal_id, details={"via": "scim", "status": new_status})
    _bump(db, org)


def _cascade(db: Any, ctx: AuthContext, principal_id: str) -> None:
    from . import delegation

    try:
        delegation.revoke_cascade(delegation.CascadeTrigger.PERSON_DISABLED, principal_id, actor=ctx,
                                  reason="disabled by SCIM", db=db)
    except NotImplementedError:
        pass  # S3's cascade; the person's sessions and keys are already refused


def _bump(db: Any, org: str) -> None:
    try:
        bump_epoch(org, db=db, reason="scim")
    except NotImplementedError:
        pass


def _apply_user_fields(db: Any, ctx: AuthContext, user: users.UserRow, payload: dict[str, Any]) -> None:
    changed = []
    name = _display_name(payload, user.display_name) if ("displayName" in payload or "name" in payload) else None
    if name and name != user.display_name:
        db.execute(text("UPDATE identity_principals SET display_name = :n, updated_at = now() WHERE id = :id"),
                   {"n": name, "id": user.principal_id})
        changed.append("display_name")
    if "userName" in payload or "emails" in payload:
        email = _email_of(payload)
        if "@" not in email:
            raise ScimError(400, "invalid email", "invalidValue")
        if email.lower() != user.email.lower():
            other = users.by_email(db, email)
            if other is not None and other.principal_id != user.principal_id:
                raise ScimError(409, "a person with this email already exists", "uniqueness")
            db.execute(text("UPDATE identity_users SET email = :e, email_verified_at = NULL WHERE principal_id = :id"),
                       {"e": email, "id": user.principal_id})
            changed.append("email")
    if changed:
        emit(db, org_id=ctx.principal.org_id, event_type="scim.user.updated", outcome=Outcome.SUCCESS, ctx=ctx,
             target_type="user", target_id=user.principal_id, details={"fields": changed})
    if "active" in payload:
        if not isinstance(payload["active"], bool):
            raise ScimError(400, "active must be a boolean", "invalidValue")
        _set_active(db, ctx, users.by_id(db, user.principal_id), payload["active"])


def replace_user(db: Any, ctx: AuthContext, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    _refuse_role_attributes(payload)
    user = _user_row(db, ctx.principal.org_id, user_id)
    if user is None:
        raise ScimError(404, "not found")
    _apply_user_fields(db, ctx, user, payload)
    return user_resource(users.by_id(db, user_id))


def patch_user(db: Any, ctx: AuthContext, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    user = _user_row(db, ctx.principal.org_id, user_id)
    if user is None:
        raise ScimError(404, "not found")
    fields: dict[str, Any] = {}
    for op in _operations(payload):
        kind = str(op.get("op", "")).lower()
        if kind not in ("replace", "add"):
            raise ScimError(400, f"op {kind!r} is not supported on users", "invalidSyntax")
        path, value = op.get("path"), op.get("value")
        if path:
            if path.split(".", 1)[0].split("[", 1)[0].lower() in FORBIDDEN_ATTRIBUTES:
                _refuse_role_attributes({path: value})
            fields[_canonical_user_attr(path)] = value
        elif isinstance(value, dict):
            _refuse_role_attributes(value)
            fields.update({_canonical_user_attr(k): v for k, v in value.items()})
        else:
            raise ScimError(400, "a patch op needs a path or an object value", "invalidSyntax")
    _refuse_role_attributes(fields)
    if "active" in fields and isinstance(fields["active"], str):
        fields["active"] = fields["active"].strip().lower() == "true"
    _apply_user_fields(db, ctx, user, fields)
    return user_resource(users.by_id(db, user_id))


def _canonical_user_attr(path: str) -> str:
    lowered = path.lower()
    return {"active": "active", "username": "userName", "displayname": "displayName",
            "name": "name", "emails": "emails"}.get(lowered, path)


def _operations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    ops = payload.get("Operations") or payload.get("operations")
    if not isinstance(ops, list) or not ops:
        raise ScimError(400, "PatchOp needs Operations", "invalidSyntax")
    return [o for o in ops if isinstance(o, dict)]


def delete_user(db: Any, ctx: AuthContext, user_id: str) -> None:
    """Deprovisioning disables (with the cascade); nothing is erased (§4.9, audit tombstones)."""
    user = _user_row(db, ctx.principal.org_id, user_id)
    if user is None:
        raise ScimError(404, "not found")
    _set_active(db, ctx, user, False)


# ---- groups --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Group:
    id: str
    name: str
    external_id: Optional[str]


def _group(db: Any, org: str, group_id: str) -> Optional[_Group]:
    r = db.execute(text("SELECT id, name, external_id FROM identity_groups WHERE id = :id AND org_id = :o "
                        "AND source = 'scim'"), {"id": group_id, "o": org}).first()
    return _Group(r.id, r.name, r.external_id) if r else None


def group_resource(db: Any, group: _Group) -> dict[str, Any]:
    members = db.execute(text("SELECT m.principal_id, p.display_name FROM identity_group_members m JOIN "
                              "identity_principals p ON p.id = m.principal_id WHERE m.group_id = :g "
                              "ORDER BY m.principal_id"), {"g": group.id})
    return {"schemas": [GROUP_SCHEMA], "id": group.id, "displayName": group.name,
            "externalId": group.external_id,
            "members": [{"value": r[0], "display": r[1]} for r in members],
            "meta": {"resourceType": "Group", "location": f"/scim/v2/Groups/{group.id}"}}


def _member_ids(db: Any, org: str, members: Any) -> list[str]:
    if members in (None, []):
        return []
    if not isinstance(members, list):
        raise ScimError(400, "members must be a list", "invalidValue")
    wanted = sorted({str(m.get("value")) for m in members if isinstance(m, dict) and m.get("value")})
    if not wanted:
        return []
    found = {r[0] for r in db.execute(text("SELECT id FROM identity_principals WHERE org_id = :o AND "
                                           "kind = 'user' AND id = ANY(:ids)"), {"o": org, "ids": wanted})}
    missing = sorted(set(wanted) - found)
    if missing:
        raise ScimError(400, "unknown member ids", "invalidValue")
    return wanted


def _add_members(db: Any, group_id: str, member_ids: Iterable[str], by: str) -> None:
    for pid in member_ids:
        db.execute(text("INSERT INTO identity_group_members (group_id, principal_id, source, added_by) "
                        "VALUES (:g, :p, 'scim', :by) ON CONFLICT DO NOTHING"), {"g": group_id, "p": pid, "by": by})


def list_groups(db: Any, ctx: AuthContext, *, filter_: Optional[str], start: int, count: int) -> dict[str, Any]:
    sql = "SELECT id, name, external_id FROM identity_groups WHERE org_id = :o AND source = 'scim'"
    params: dict[str, Any] = {"o": ctx.principal.org_id}
    if filter_:
        _attr, value = _parse_eq_filter(filter_, {"displayname", "externalid"})
        sql += " AND (name = :v OR external_id = :v)"
        params["v"] = value
    rows = [_Group(r.id, r.name, r.external_id) for r in db.execute(text(sql + " ORDER BY created_at, id"), params)]
    start = max(1, start)
    count = max(0, min(count, MAX_COUNT))
    page = rows[start - 1:start - 1 + count]
    return {"schemas": [LIST_SCHEMA], "totalResults": len(rows), "startIndex": start,
            "itemsPerPage": len(page), "Resources": [group_resource(db, g) for g in page]}


def create_group(db: Any, ctx: AuthContext, payload: dict[str, Any]) -> dict[str, Any]:
    _refuse_role_attributes(payload)
    name = str(payload.get("displayName") or "").strip()[:200]
    if not name:
        raise ScimError(400, "displayName is required", "invalidValue")
    org = ctx.principal.org_id
    external = str(payload.get("externalId") or name)[:200]
    gid = ids.new_id("grp")
    key = f"scim-{gid[4:]}"
    db.execute(text("INSERT INTO identity_groups (id, org_id, key, name, source, external_id) "
                    "VALUES (:id, :o, :k, :n, 'scim', :e)"), {"id": gid, "o": org, "k": key, "n": name, "e": external})
    _add_members(db, gid, _member_ids(db, org, payload.get("members")), ctx.principal.id)
    emit(db, org_id=org, event_type="scim.group.created", outcome=Outcome.SUCCESS, ctx=ctx,
         target_type="group", target_id=gid)
    _bump(db, org)
    return group_resource(db, _group(db, org, gid))


def get_group(db: Any, ctx: AuthContext, group_id: str) -> dict[str, Any]:
    group = _group(db, ctx.principal.org_id, group_id)
    if group is None:
        raise ScimError(404, "not found")
    return group_resource(db, group)


def replace_group(db: Any, ctx: AuthContext, group_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    _refuse_role_attributes(payload)
    org = ctx.principal.org_id
    group = _group(db, org, group_id)
    if group is None:
        raise ScimError(404, "not found")
    if payload.get("displayName"):
        db.execute(text("UPDATE identity_groups SET name = :n WHERE id = :id"),
                   {"n": str(payload["displayName"])[:200], "id": group_id})
    if "members" in payload:
        wanted = _member_ids(db, org, payload.get("members"))
        db.execute(text("DELETE FROM identity_group_members WHERE group_id = :g AND NOT (principal_id = ANY(:keep))"),
                   {"g": group_id, "keep": wanted})
        _add_members(db, group_id, wanted, ctx.principal.id)
    emit(db, org_id=org, event_type="scim.group.updated", outcome=Outcome.SUCCESS, ctx=ctx,
         target_type="group", target_id=group_id)
    _bump(db, org)
    return group_resource(db, _group(db, org, group_id))


def patch_group(db: Any, ctx: AuthContext, group_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    org = ctx.principal.org_id
    group = _group(db, org, group_id)
    if group is None:
        raise ScimError(404, "not found")
    for op in _operations(payload):
        kind = str(op.get("op", "")).lower()
        path = str(op.get("path") or "")
        value = op.get("value")
        root = path.split("[", 1)[0].split(".", 1)[0].lower()
        if root in FORBIDDEN_ATTRIBUTES or (isinstance(value, dict) and any(
                k.lower() in FORBIDDEN_ATTRIBUTES for k in value)):
            raise ScimError(400, "SCIM cannot set access roles", "invalidValue")
        if root == "members" and kind == "add":
            _add_members(db, group_id, _member_ids(db, org, value), ctx.principal.id)
        elif root == "members" and kind == "remove":
            if "[" in path:   # members[value eq "usr_x"]
                inner = path[path.index("[") + 1:path.rindex("]")]
                _attr, member = _parse_eq_filter(inner, {"value"})
                db.execute(text("DELETE FROM identity_group_members WHERE group_id = :g AND principal_id = :p"),
                           {"g": group_id, "p": member})
            else:
                removed = _member_ids(db, org, value) if value else None
                if removed is None:
                    db.execute(text("DELETE FROM identity_group_members WHERE group_id = :g"), {"g": group_id})
                else:
                    db.execute(text("DELETE FROM identity_group_members WHERE group_id = :g AND principal_id = ANY(:p)"),
                               {"g": group_id, "p": removed})
        elif root == "members" and kind == "replace":
            replace_group(db, ctx, group_id, {"members": value or []})
        elif kind == "replace" and (root == "displayname" or (not path and isinstance(value, dict))):
            name = value if root == "displayname" else (value or {}).get("displayName")
            if name:
                db.execute(text("UPDATE identity_groups SET name = :n WHERE id = :id"),
                           {"n": str(name)[:200], "id": group_id})
        else:
            raise ScimError(400, f"unsupported group patch {kind} {path}", "invalidSyntax")
    emit(db, org_id=org, event_type="scim.group.updated", outcome=Outcome.SUCCESS, ctx=ctx,
         target_type="group", target_id=group_id)
    _bump(db, org)
    return group_resource(db, _group(db, org, group_id))


def delete_group(db: Any, ctx: AuthContext, group_id: str) -> None:
    org = ctx.principal.org_id
    if _group(db, org, group_id) is None:
        raise ScimError(404, "not found")
    db.execute(text("DELETE FROM identity_group_members WHERE group_id = :g"), {"g": group_id})
    db.execute(text("DELETE FROM identity_role_bindings WHERE subject_type = 'group' AND subject_id = :g"),
               {"g": group_id})
    db.execute(text("DELETE FROM identity_groups WHERE id = :g"), {"g": group_id})
    emit(db, org_id=org, event_type="scim.group.deleted", outcome=Outcome.SUCCESS, ctx=ctx,
         target_type="group", target_id=group_id)
    _bump(db, org)
