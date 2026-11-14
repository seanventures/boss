"""SCIM 2.0 provisioning at org level (IDENTITY.md §4.9; RFC 7643/7644 subset). Stream S1.

  - Callers hold a `boss_scim_` key of a `sys_scim_<provider>` principal; that key can only use
    `scim:provision` (the PDP, plus a defence-in-depth check in the router).
  - People created over SCIM are `pending` (no password) until their first SSO sign-in.
  - `active=false` disables the person with the full cascade: sessions revoked, grants and run
    tokens revoked (S3's revoke_cascade). `active=true` re-enables (active again only for someone
    who has signed in before; otherwise back to pending).
  - Groups land read-only in BOSS (`source=scim`). SCIM can NEVER assign access roles: a request
    carrying `roles`/`entitlements` is refused (400), never silently ignored.
  - A pushed `password` (Okta's "Sync password") is IGNORED, never stored: BOSS people sign in
    through the IdP. No 400 (Okta would stop provisioning); a `scim.password_ignored` event says so.
  - A person BOSS already had is SCIM's to change only with the provider's opt-in
    `scim_adopt_existing_by_domain` (off by default; BOSSAPP-1048): their email is verified and in
    the provider's `domains`, and they are not an Owner or Admin. The first such write is a
    `scim.user.adopted` event; from then on SCIM may update and deprovision them and push them
    into groups, like the people it created.
  - Discovery: /ServiceProviderConfig, /Schemas and /ResourceTypes describe exactly this subset.
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
FORBIDDEN_ATTRIBUTES = frozenset({"roles", "entitlements", "x509certificates"})
#: Accepted and dropped (never stored, never a 400): BOSS people sign in through the IdP.
IGNORED_ATTRIBUTES = frozenset({"password"})
MAX_COUNT = 200


def _guard_owner_reach(db: Any, org: str, *, removals: Iterable[tuple[str, str]] = (),
                       disabling: Optional[str] = None) -> None:
    """H4: SCIM may not remove the last Owner's reach — by dropping a member from an Owner
    group or by deactivating the last Owner. The same check (and lock) as every other path
    (bindings.guard_membership_removals / assert_owner_remains); the deferred trigger is the
    backstop. Answered as a SCIM 409 mutability error, never a 500 at commit."""
    from .bindings import assert_owner_remains, guard_membership_removals, lock_owners, owner_ids
    from .roles import AccessError

    try:
        guard_membership_removals(db, org, list(removals))
        if disabling is not None and disabling in owner_ids(db, org):
            lock_owners(db, org)
            assert_owner_remains(db, org, without_principal=disabling)
    except AccessError:
        raise ScimError(409, "This would leave the organisation without an Owner. Add another Owner first.",
                        "mutability") from None


def _members_of(db: Any, group_id: str) -> list[str]:
    return [r[0] for r in db.execute(text("SELECT principal_id FROM identity_group_members WHERE group_id = :g"),
                                     {"g": group_id})]


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


def _drop_ignored(db: Any, ctx: AuthContext, payload: dict[str, Any], target: Optional[str] = None) -> dict[str, Any]:
    """The payload without a pushed password; the event records THAT one was ignored, never it."""
    dropped = [k for k in payload if k.lower() in IGNORED_ATTRIBUTES]
    if not dropped:
        return payload
    import logging

    logging.getLogger("boss.identity.scim").info("scim: ignored a pushed password (BOSS stores none)")
    emit(db, org_id=ctx.principal.org_id, event_type="scim.password_ignored", outcome=Outcome.INFO, ctx=ctx,
         target_type="user" if target else None, target_id=target, details={"attribute": "password"})
    return {k: v for k, v in payload.items() if k.lower() not in IGNORED_ATTRIBUTES}


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


def provisioning_principal(provider_id: str) -> str:
    """The `sys_scim_<provider>` principal a provider's SCIM keys belong to: `created_by` of every
    person that connection provisioned."""
    return "sys_scim_" + str(provider_id).split("_", 1)[-1]


#: Roles a SCIM key can never touch the holder of (M2): their account is BOSS's to manage.
PROTECTED_ROLES = frozenset({"owner", "admin"})


def provider_of(db: Any, ctx: AuthContext) -> Any:
    """The identity provider whose SCIM connection this caller is (sys_scim_<x> ↔ idp_<x>)."""
    from .sso_oidc import load_provider

    pid = ctx.principal.id
    if not pid.startswith("sys_scim_"):
        return None
    provider = load_provider(db, "idp_" + pid[len("sys_scim_"):], active_only=False)
    return provider if provider and provider.org_id == ctx.principal.org_id else None


def _adoptable(db: Any, ctx: AuthContext, user: users.UserRow) -> bool:
    """scim_adopt_existing_by_domain (off by default): a VERIFIED email in a domain the admin
    listed on this connection's provider. Owners and Admins are checked by the caller."""
    provider = provider_of(db, ctx)
    if provider is None or not provider.scim_adopt_existing_by_domain or user.email_verified_at is None:
        return False
    domain = user.email.rsplit("@", 1)[1].lower() if "@" in user.email else ""
    return bool(domain) and domain in {d.lower().lstrip("@") for d in (provider.domains or ())}


def _writable_user(db: Any, ctx: AuthContext, user_id: str) -> users.UserRow:
    """The person a SCIM WRITE may change (M2): one THIS provisioning principal created (or, with
    the provider's opt-in, adopted by verified domain), and never an Owner or Admin. Anyone else —
    people invited in BOSS, the claimant, another provider's — is refused, so a SCIM key cannot
    rewrite an Owner's email and reset their password."""
    user = _user_row(db, ctx.principal.org_id, user_id)
    if user is None:
        raise ScimError(404, "not found")
    created_by = db.execute(text("SELECT created_by FROM identity_principals WHERE id = :id"),
                            {"id": user.principal_id}).scalar()
    adopting = created_by != ctx.principal.id and _adoptable(db, ctx, user)
    if created_by != ctx.principal.id and not adopting:
        raise ScimError(403, "this person was not provisioned by this SCIM connection; change them in BOSS")
    if PROTECTED_ROLES.intersection(users.role_keys(db, user.principal_id)):
        raise ScimError(403, "SCIM cannot change an Owner or an Admin; change them in BOSS")
    if adopting:
        adopted = db.execute(text("UPDATE identity_users SET scim_adopted_by = :by WHERE principal_id = :p "
                                  "AND scim_adopted_by IS DISTINCT FROM :by"),
                             {"by": ctx.principal.id, "p": user.principal_id}).rowcount
        if adopted:
            emit(db, org_id=ctx.principal.org_id, event_type="scim.user.adopted", outcome=Outcome.SUCCESS,
                 ctx=ctx, target_type="user", target_id=user.principal_id,
                 details={"rule": "scim_adopt_existing_by_domain"})
    return user


def user_resource(user: users.UserRow, *, base: str = "", given: Optional[str] = None,
                  family: Optional[str] = None, external_id: Optional[str] = None) -> dict[str, Any]:
    name: dict[str, Any] = {"formatted": user.display_name}
    if given:
        name["givenName"] = given
    if family:
        name["familyName"] = family
    out = {
        "schemas": [USER_SCHEMA], "id": user.principal_id, "userName": user.email,
        "displayName": user.display_name, "name": name,
        "emails": [{"value": user.email, "primary": True, "type": "work"}],
        "active": user.status in ("active", "pending"),
        "meta": {"resourceType": "User", "location": f"{base}/scim/v2/Users/{user.principal_id}"},
    }
    if external_id:
        out["externalId"] = external_id
    return out


def _resource(db: Any, user: Optional[users.UserRow]) -> dict[str, Any]:
    """The User resource with the SCIM-only fields (name parts, externalId) BOSS stores."""
    if user is None:
        raise ScimError(404, "not found")
    r = db.execute(text("SELECT given_name, family_name, scim_external_id FROM identity_users "
                        "WHERE principal_id = :p"), {"p": user.principal_id}).first()
    return user_resource(user, given=r.given_name if r else None, family=r.family_name if r else None,
                         external_id=r.scim_external_id if r else None)


def list_users(db: Any, ctx: AuthContext, *, filter_: Optional[str], start: int, count: int
               ) -> dict[str, Any]:
    org = ctx.principal.org_id
    sql = ("SELECT p.id FROM identity_principals p JOIN identity_users u ON u.principal_id = p.id "
           "WHERE p.org_id = :o AND p.kind = 'user' AND NOT p.network_bound")
    params: dict[str, Any] = {"o": org}
    if filter_:
        attr, value = _parse_eq_filter(filter_, {"username", "emails.value", "emails", "externalid"})
        if attr == "externalid":
            sql += " AND u.scim_external_id = :v"
        else:
            sql += " AND lower(u.email) = lower(:v)"
        params["v"] = value
    ids_ = [r[0] for r in db.execute(text(sql + " ORDER BY p.created_at, p.id"), params)]
    start = max(1, start)
    count = max(0, min(count, MAX_COUNT))
    page = ids_[start - 1:start - 1 + count]
    resources = [_resource(db, u) for u in (users.by_id(db, i) for i in page) if u]
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
    payload = _drop_ignored(db, ctx, payload)
    email = _email_of(payload)
    if "@" not in email or len(email) > 320:
        raise ScimError(400, "userName or a primary email address is required", "invalidValue")
    existing = users.by_email(db, email)
    if existing is not None:
        if existing.org_id == ctx.principal.org_id and not existing.network_bound and _adoptable(db, ctx, existing):
            # Opt-in adoption: the person BOSS already had becomes this connection's to manage
            # (an Owner or Admin never: _writable_user refuses them with words).
            user = _writable_user(db, ctx, existing.principal_id)
            _apply_user_fields(db, ctx, user, payload)
            return _resource(db, users.by_id(db, user.principal_id))
        raise ScimError(409, "a person with this email already exists", "uniqueness")
    pid = users.create_user(db, org_id=ctx.principal.org_id, email=email,
                            display_name=_display_name(payload, email), password_hash=None,
                            status="pending", created_by=ctx.principal.id)
    _store_scim_fields(db, pid, payload)
    emit(db, org_id=ctx.principal.org_id, event_type="scim.user.created", outcome=Outcome.SUCCESS, ctx=ctx,
         target_type="user", target_id=pid)
    if payload.get("active") is False:
        _set_active(db, ctx, users.by_id(db, pid), False)
    return _resource(db, users.by_id(db, pid))


def _store_scim_fields(db: Any, principal_id: str, payload: dict[str, Any]) -> list[str]:
    """externalId and the name parts, as given (only the keys the payload carries)."""
    sets: dict[str, Any] = {}
    if "externalId" in payload:
        value = payload.get("externalId")
        sets["scim_external_id"] = str(value)[:300] if value not in (None, "") else None
    name = payload.get("name")
    if isinstance(name, dict):
        for key, column in (("givenName", "given_name"), ("familyName", "family_name")):
            if key in name:
                sets[column] = str(name[key])[:200] if name[key] not in (None, "") else None
    for column, value in sets.items():     # columns come from the fixed lists above only
        db.execute(text(f"UPDATE identity_users SET {column} = :v WHERE principal_id = :p"),
                   {"v": value, "p": principal_id})
    return sorted(sets)


def get_user(db: Any, ctx: AuthContext, user_id: str) -> dict[str, Any]:
    user = _user_row(db, ctx.principal.org_id, user_id)
    if user is None:
        raise ScimError(404, "not found")
    return _resource(db, user)


def _set_active(db: Any, ctx: AuthContext, user: Optional[users.UserRow], active: bool) -> None:
    if user is None:
        return
    org = ctx.principal.org_id
    if not active:
        if user.status == "disabled":
            return
        _guard_owner_reach(db, org, disabling=user.principal_id)
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
        disabled_by = db.execute(text("SELECT status_changed_by FROM identity_principals WHERE id = :id"),
                                 {"id": user.principal_id}).scalar()
        if disabled_by != ctx.principal.id:
            # An admin disabled this person in BOSS: SCIM's active=true does not undo that.
            raise ScimError(409, "this person was disabled in BOSS; an admin re-enables them there")
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
    changed: list[str] = []
    if isinstance(payload.get("name"), dict) and not payload.get("displayName") and not payload["name"].get("formatted"):
        # A partial name (one part patched) keeps the other stored part for the display name.
        r = db.execute(text("SELECT given_name, family_name FROM identity_users WHERE principal_id = :p"),
                       {"p": user.principal_id}).first()
        stored = {"givenName": r.given_name, "familyName": r.family_name} if r else {}
        payload = {**payload, "name": {**{k: v for k, v in stored.items() if v}, **payload["name"]}}
    changed += _store_scim_fields(db, user.principal_id, payload)
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
            users.change_email(db, user, email, by=ctx.principal.id)
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
    user = _writable_user(db, ctx, user_id)
    payload = _drop_ignored(db, ctx, payload, user.principal_id)
    _apply_user_fields(db, ctx, user, payload)
    return _resource(db, users.by_id(db, user_id))


def patch_user(db: Any, ctx: AuthContext, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """PATCH replace / add, with or without a path (Okta's and Entra's shapes): `active`,
    `userName`, `displayName`, `name` or `name.givenName` / `name.familyName`, `emails` or
    `emails[type eq "work"].value`, `externalId`. A pushed password is ignored."""
    user = _writable_user(db, ctx, user_id)
    fields: dict[str, Any] = {}
    for op in _operations(payload):
        kind = str(op.get("op", "")).lower()
        if kind not in ("replace", "add"):
            raise ScimError(400, f"op {kind!r} is not supported on users", "invalidSyntax")
        path, value = op.get("path"), op.get("value")
        if path:
            if str(path).split(".", 1)[0].split("[", 1)[0].lower() in FORBIDDEN_ATTRIBUTES:
                _refuse_role_attributes({path: value})
            _set_user_attr(fields, str(path), value)
        elif isinstance(value, dict):
            _refuse_role_attributes(value)
            for key, item in value.items():
                _set_user_attr(fields, str(key), item)
        else:
            raise ScimError(400, "a patch op needs a path or an object value", "invalidSyntax")
    _refuse_role_attributes(fields)
    fields = _drop_ignored(db, ctx, fields, user.principal_id)
    if "active" in fields and isinstance(fields["active"], str):
        fields["active"] = fields["active"].strip().lower() == "true"
    _apply_user_fields(db, ctx, user, fields)
    return _resource(db, users.by_id(db, user_id))


def _set_user_attr(fields: dict[str, Any], path: str, value: Any) -> None:
    """One patched attribute into the fields `_apply_user_fields` reads."""
    if path.startswith(USER_SCHEMA + ":"):           # a fully qualified path
        path = path[len(USER_SCHEMA) + 1:]
    root, _, sub = path.partition(".")
    key = root.split("[", 1)[0].lower()
    if key == "name" and sub:
        part = {"givenname": "givenName", "familyname": "familyName", "formatted": "formatted"}.get(sub.lower())
        if part:
            fields.setdefault("name", {})[part] = value
        return
    if key == "name" and isinstance(value, dict):
        fields.setdefault("name", {}).update(value)
        return
    if key == "emails":
        if isinstance(value, str):
            fields["emails"] = [{"value": value, "primary": True}]
        elif isinstance(value, list):
            fields["emails"] = value
        elif isinstance(value, dict) and value.get("value"):
            fields["emails"] = [value]
        return
    fields[_canonical_user_attr(root)] = value


def _canonical_user_attr(path: str) -> str:
    lowered = path.lower()
    return {"active": "active", "username": "userName", "displayname": "displayName",
            "name": "name", "emails": "emails", "externalid": "externalId",
            "password": "password"}.get(lowered, path)


def _operations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    ops = payload.get("Operations") or payload.get("operations")
    if not isinstance(ops, list) or not ops:
        raise ScimError(400, "PatchOp needs Operations", "invalidSyntax")
    return [o for o in ops if isinstance(o, dict)]


def delete_user(db: Any, ctx: AuthContext, user_id: str) -> None:
    """Deprovisioning disables (with the cascade); nothing is erased (§4.9, audit tombstones)."""
    user = _writable_user(db, ctx, user_id)
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


def _group_grants_protected_role(db: Any, group_id: str) -> bool:
    """Does a binding (made by an admin in BOSS) give this group a protected role?"""
    return db.execute(text(
        "SELECT 1 FROM identity_role_bindings b JOIN identity_access_roles r ON r.id = b.role_id "
        "WHERE b.subject_type = 'group' AND b.subject_id = :g AND r.key = ANY(:keys) LIMIT 1"),
        {"g": group_id, "keys": sorted(PROTECTED_ROLES)}).first() is not None


def _guard_member_writes(db: Any, ctx: AuthContext, group_id: str, *, adding: Iterable[str] = (),
                         removing: Iterable[str] = ()) -> None:
    """Integration round 2 (c): a SCIM group membership write has the SAME limits as a SCIM user
    write (M2): it may only move people THIS connection provisioned and never an Owner or Admin,
    and it may never ADD anyone to a group an admin bound to Owner or Admin — otherwise a SCIM key
    (or a compromised IdP) places Owners by group membership."""
    adding, removing = list(adding), list(removing)
    if adding and _group_grants_protected_role(db, group_id):
        raise ScimError(403, "this group grants Owner or Admin in BOSS; SCIM cannot add people to it "
                             "— change its members in BOSS")
    for pid in dict.fromkeys(adding + removing):
        _writable_user(db, ctx, pid)


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
    first = _member_ids(db, org, payload.get("members"))
    _guard_member_writes(db, ctx, gid, adding=first)
    _add_members(db, gid, first, ctx.principal.id)
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
        current = _members_of(db, group_id)
        _guard_owner_reach(db, org, removals=[(group_id, p) for p in current if p not in wanted])
        _guard_member_writes(db, ctx, group_id, adding=[p for p in wanted if p not in current],
                             removing=[p for p in current if p not in wanted])
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
            adding = [p for p in _member_ids(db, org, value) if p not in _members_of(db, group_id)]
            _guard_member_writes(db, ctx, group_id, adding=adding)
            _add_members(db, group_id, adding, ctx.principal.id)
        elif root == "members" and kind == "remove":
            if "[" in path:   # members[value eq "usr_x"]
                inner = path[path.index("[") + 1:path.rindex("]")]
                _attr, member = _parse_eq_filter(inner, {"value"})
                _guard_owner_reach(db, org, removals=[(group_id, member)])
                _guard_member_writes(db, ctx, group_id, removing=[member])
                db.execute(text("DELETE FROM identity_group_members WHERE group_id = :g AND principal_id = :p"),
                           {"g": group_id, "p": member})
            else:
                removed = _member_ids(db, org, value) if value else None
                _guard_owner_reach(db, org, removals=[(group_id, p) for p in (
                    _members_of(db, group_id) if removed is None else removed)])
                _guard_member_writes(db, ctx, group_id,
                                     removing=_members_of(db, group_id) if removed is None else removed)
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
    _guard_owner_reach(db, org, removals=[(group_id, p) for p in _members_of(db, group_id)])
    db.execute(text("DELETE FROM identity_group_members WHERE group_id = :g"), {"g": group_id})
    db.execute(text("DELETE FROM identity_role_bindings WHERE subject_type = 'group' AND subject_id = :g"),
               {"g": group_id})
    db.execute(text("DELETE FROM identity_groups WHERE id = :g"), {"g": group_id})
    emit(db, org_id=org, event_type="scim.group.deleted", outcome=Outcome.SUCCESS, ctx=ctx,
         target_type="group", target_id=group_id)
    _bump(db, org)


# ---- discovery (RFC 7643 §5-§7; what Okta's SCIM test suite reads first) -------------------------

SPC_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
RESOURCE_TYPE_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ResourceType"
SCHEMA_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Schema"


def service_provider_config() -> dict[str, Any]:
    """Exactly the subset served: PATCH yes; `eq` filters on userName / emails / externalId
    (users) and displayName / externalId (groups); no bulk, sort, ETags or password change."""
    return {
        "schemas": [SPC_SCHEMA],
        "documentationUri": "https://boss.sean.network/docs/SIGN_IN_SETUP/#okta",
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": MAX_COUNT},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [{
            "type": "oauthbearertoken", "name": "OAuth Bearer Token", "primary": True,
            "description": "A BOSS SCIM key (boss_scim_…) from Identity › Sign-in & SSO › the provider › SCIM",
        }],
        "meta": {"resourceType": "ServiceProviderConfig", "location": "/scim/v2/ServiceProviderConfig"},
    }


def _attr(name: str, type_: str = "string", *, multi: bool = False, required: bool = False,
          mutability: str = "readWrite", uniqueness: str = "none", sub: Optional[list] = None,
          returned: str = "default") -> dict[str, Any]:
    out: dict[str, Any] = {"name": name, "type": type_, "multiValued": multi, "required": required,
                           "caseExact": False, "mutability": mutability, "returned": returned,
                           "uniqueness": uniqueness}
    if sub:
        out["subAttributes"] = sub
    return out


def schemas() -> list[dict[str, Any]]:
    user = {"schemas": [SCHEMA_SCHEMA], "id": USER_SCHEMA, "name": "User", "description": "A BOSS person",
            "attributes": [
                _attr("userName", required=True, uniqueness="server"),
                _attr("name", "complex", sub=[_attr("formatted"), _attr("givenName"), _attr("familyName")]),
                _attr("displayName"),
                _attr("emails", "complex", multi=True, sub=[_attr("value"), _attr("type"), _attr("primary", "boolean")]),
                _attr("active", "boolean"),
                _attr("externalId"),
                # Accepted and ignored: BOSS people sign in through the IdP.
                _attr("password", mutability="writeOnly", returned="never"),
            ],
            "meta": {"resourceType": "Schema", "location": f"/scim/v2/Schemas/{USER_SCHEMA}"}}
    group = {"schemas": [SCHEMA_SCHEMA], "id": GROUP_SCHEMA, "name": "Group", "description": "A BOSS group",
             "attributes": [
                 _attr("displayName", required=True),
                 _attr("externalId"),
                 _attr("members", "complex", multi=True, sub=[_attr("value", mutability="immutable"),
                                                             _attr("display", mutability="readOnly")]),
             ],
             "meta": {"resourceType": "Schema", "location": f"/scim/v2/Schemas/{GROUP_SCHEMA}"}}
    return [user, group]


def resource_types() -> list[dict[str, Any]]:
    return [
        {"schemas": [RESOURCE_TYPE_SCHEMA], "id": "User", "name": "User", "endpoint": "/Users",
         "schema": USER_SCHEMA, "meta": {"resourceType": "ResourceType", "location": "/scim/v2/ResourceTypes/User"}},
        {"schemas": [RESOURCE_TYPE_SCHEMA], "id": "Group", "name": "Group", "endpoint": "/Groups",
         "schema": GROUP_SCHEMA, "meta": {"resourceType": "ResourceType", "location": "/scim/v2/ResourceTypes/Group"}},
    ]


def listed(resources: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schemas": [LIST_SCHEMA], "totalResults": len(resources), "startIndex": 1,
            "itemsPerPage": len(resources), "Resources": resources}
