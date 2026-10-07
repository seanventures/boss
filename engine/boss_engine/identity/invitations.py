"""Invitations (IDENTITY.md §4.8 "Invitation", §6.11 "Invite with roles"; J3).

An invitation names an email, the access roles (role key + scope) and manual groups the person
gets, and is single use for 7 days. The invited roles must be a subset of the inviter's own
permissions at each scope (no escalation by invitation). The token is 32 random bytes, SHA-256 at
rest; with SMTP it is emailed, otherwise it is shown ONCE to the authenticated inviter to hand
over out of band — never from a public endpoint (Kaappu's flaw).

An invitation is also the ONLY email-based binding of a federated identity, and only to an
unclaimed invitation, never to an existing account (pre-account takeover, A61, A89).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import text

from . import ids, users
from .audit import Outcome
from .authn_support import emit, random_token, sha256_hex, utcnow
from .principal import AuthContext

TTL_DAYS = 7


class InvitationRefused(Exception):
    def __init__(self, code: str, status: int = 422, detail: Optional[dict] = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.detail = detail or {}


@dataclass(frozen=True)
class Invitation:
    id: str
    org_id: str
    email: str
    bindings: list[dict[str, Any]]
    group_ids: list[str]
    invited_by: str
    expires_at: datetime
    accepted_at: Optional[datetime]
    revoked_at: Optional[datetime]


def _role_permissions(db: Any, role_key: str, org_id: str) -> Optional[set[str]]:
    rid = db.execute(text("SELECT id FROM identity_access_roles WHERE key = :k AND (org_id IS NULL "
                          "OR org_id = :o) ORDER BY org_id NULLS FIRST LIMIT 1"),
                     {"k": role_key, "o": org_id}).scalar()
    if rid is None:
        return None
    return {r[0] for r in db.execute(text("SELECT permission_key FROM identity_access_role_permissions "
                                          "WHERE role_id = :r"), {"r": rid})}


def _check_subset(db: Any, inviter: AuthContext, bindings: list[dict[str, Any]]) -> None:
    from . import authz
    from .scope import ResolvedScope

    org_id = inviter.principal.org_id
    for b in bindings:
        perms = _role_permissions(db, b["role_key"], org_id)
        if perms is None:
            raise InvitationRefused("unknown_access_role", 422, {"role_key": b["role_key"]})
        if b["role_key"] == "owner":
            # Making an Owner needs owner:assign, held by the inviter at org scope.
            perms = perms | {"owner:assign"}
        scope = (ResolvedScope.org_level(org_id) if b["scope_type"] == "org"
                 else ResolvedScope.in_project(org_id, b["scope_id"]))
        held = authz.effective_permissions(inviter, scope, db=db).permissions
        missing = sorted(perms - set(held))
        if missing:
            raise InvitationRefused("roles_exceed_inviter", 403, {"permissions": missing[:20]})


def _check_groups(db: Any, inviter: AuthContext, group_ids: list[str]) -> None:
    """C1: inviting someone into a group IS granting what the group's bindings carry — the same
    ceiling as adding a member (groups.membership_ceilings: A34, and owner:assign for an Owner
    group). An invitation is never a way round it."""
    if not group_ids:
        return
    from .groups import membership_ceilings
    from .roles import AccessError

    try:
        membership_ceilings(db, inviter, group_ids)
    except AccessError as exc:
        if exc.status in (404, 409):           # not this org's, or not a manual group
            raise InvitationRefused("unknown_group", 422) from None
        raise InvitationRefused("groups_exceed_inviter", 403,
                                {k: v for k, v in exc.extra.items() if k != "reason"}) from None


def inviter_context(db: Any, inv: "Invitation") -> AuthContext:
    """The inviter as they are NOW (accept re-runs every ceiling with it): an inviter who was
    disabled, deleted or moved to another org grants nothing."""
    from .principal import AuthMethod
    from .users import load_principal

    principal = load_principal(db, inv.invited_by)
    if principal is None or principal.org_id != inv.org_id or not principal.active:
        raise InvitationRefused("invitation_invalid", 400, {"reason": "inviter_inactive"})
    # A re-check of what the inviter HOLDS (no request of theirs is in flight, so step-up
    # freshness is not the question here; it was asked when they created the invitation).
    return AuthContext(principal=principal, method=AuthMethod.SYSTEM)


def _normalise_bindings(raw: Iterable[Any]) -> list[dict[str, Any]]:
    out = []
    for b in raw or []:
        if not isinstance(b, dict) or set(b) - {"role_key", "scope_type", "scope_id"}:
            raise InvitationRefused("invalid_binding", 422)
        scope_type = b.get("scope_type", "org")
        if scope_type not in ("org", "project") or not b.get("role_key"):
            raise InvitationRefused("invalid_binding", 422)
        if scope_type == "project" and not b.get("scope_id"):
            raise InvitationRefused("invalid_binding", 422)
        out.append({"role_key": str(b["role_key"]), "scope_type": scope_type,
                    "scope_id": str(b.get("scope_id") or "") or None})
    return out


def create(db: Any, *, inviter: AuthContext, email: str, bindings: Iterable[Any],
           group_ids: Iterable[str] = ()) -> tuple[Invitation, str]:
    """Returns (invitation, token). The token is for the email or the shown-once dialog only."""
    email = (email or "").strip()
    if "@" not in email or len(email) > 320:
        raise InvitationRefused("invalid_email", 422)
    if users.by_email(db, email):
        raise InvitationRefused("email_in_use", 409)
    clean = _normalise_bindings(bindings)
    _check_subset(db, inviter, clean)
    groups = sorted(set(group_ids or ()))
    if groups:
        found = {r[0] for r in db.execute(text("SELECT id FROM identity_groups WHERE org_id = :o AND "
                                               "source = 'manual' AND id = ANY(:g)"),
                                          {"o": inviter.principal.org_id, "g": groups})}
        if found != set(groups):
            raise InvitationRefused("unknown_group", 422)
    _check_groups(db, inviter, groups)
    # One open invitation per address: a new one replaces the old.
    db.execute(text("UPDATE identity_invitations SET revoked_at = now() WHERE org_id = :o AND "
                    "lower(email) = lower(:e) AND accepted_at IS NULL AND revoked_at IS NULL"),
               {"o": inviter.principal.org_id, "e": email})
    token = random_token("binv_")
    iid = ids.new_id("inv")
    expires = utcnow() + timedelta(days=TTL_DAYS)
    db.execute(text("INSERT INTO identity_invitations (id, org_id, email, token_hash, bindings, group_ids, "
                    "invited_by, expires_at) VALUES (:id, :o, :e, :th, CAST(:b AS jsonb), :g, :by, :exp)"),
               {"id": iid, "o": inviter.principal.org_id, "e": email, "th": sha256_hex(token),
                "b": json.dumps(clean), "g": groups, "by": inviter.principal.id, "exp": expires})
    emit(db, org_id=inviter.principal.org_id, event_type="user.invited", outcome=Outcome.SUCCESS,
         ctx=inviter, target_type="invitation", target_id=iid,
         details={"bindings": clean, "groups": groups})
    inv = Invitation(iid, inviter.principal.org_id, email, clean, groups, inviter.principal.id,
                     expires, None, None)
    return inv, token


def _row(r: Any) -> Invitation:
    bindings = r.bindings if isinstance(r.bindings, list) else json.loads(r.bindings or "[]")
    return Invitation(r.id, r.org_id, r.email, bindings, list(r.group_ids or []), r.invited_by,
                      r.expires_at, r.accepted_at, r.revoked_at)


def open_by_token(db: Any, token: str) -> Optional[Invitation]:
    if not token or len(token) > 200:
        return None
    r = db.execute(text("SELECT * FROM identity_invitations WHERE token_hash = :th AND accepted_at IS NULL "
                        "AND revoked_at IS NULL AND expires_at > now() FOR UPDATE"),
                   {"th": sha256_hex(token)}).first()
    return _row(r) if r else None


def open_for_email(db: Any, email: str) -> Optional[Invitation]:
    """The open, unclaimed invitation for a VERIFIED federated email (the only email binding)."""
    r = db.execute(text("SELECT * FROM identity_invitations WHERE lower(email) = lower(:e) AND "
                        "accepted_at IS NULL AND revoked_at IS NULL AND expires_at > now() "
                        "ORDER BY created_at DESC LIMIT 1 FOR UPDATE"), {"e": (email or "").strip()}).first()
    return _row(r) if r else None


def list_open(db: Any, org_id: str) -> list[Invitation]:
    rows = db.execute(text("SELECT * FROM identity_invitations WHERE org_id = :o AND accepted_at IS NULL "
                           "AND revoked_at IS NULL AND expires_at > now() ORDER BY created_at DESC"),
                      {"o": org_id})
    return [_row(r) for r in rows]


def revoke(db: Any, *, actor: AuthContext, invitation_id: str) -> bool:
    return db.execute(text("UPDATE identity_invitations SET revoked_at = now() WHERE id = :id AND org_id = :o "
                           "AND accepted_at IS NULL AND revoked_at IS NULL RETURNING id"),
                      {"id": invitation_id, "o": actor.principal.org_id}).first() is not None


def accept(db: Any, inv: Invitation, *, display_name: str, password_hash: Optional[str],
           email_verified: bool) -> str:
    """Create the person, apply the invited bindings and groups, and close the invitation (single
    use: the UPDATE only lands once)."""
    # C1: the ceilings are re-checked against the inviter as they are now — a demoted or
    # disabled inviter's invitation grants nothing it no longer could. Before anything is
    # written, so a refusal leaves the invitation as it was.
    inviter = inviter_context(db, inv)
    _check_subset(db, inviter, list(inv.bindings))
    _check_groups(db, inviter, list(inv.group_ids))
    closed = db.execute(text("UPDATE identity_invitations SET accepted_at = now() WHERE id = :id AND "
                             "accepted_at IS NULL AND revoked_at IS NULL RETURNING id"),
                        {"id": inv.id}).first()
    if closed is None:
        raise InvitationRefused("invitation_invalid", 400)
    if users.by_email(db, inv.email):
        raise InvitationRefused("email_in_use", 409)
    pid = users.create_user(db, org_id=inv.org_id, email=inv.email, display_name=display_name or inv.email,
                            password_hash=password_hash, email_verified=email_verified,
                            created_by=inv.invited_by)
    for b in inv.bindings:
        users.add_binding(db, org_id=inv.org_id, subject_id=pid, role_key=b["role_key"],
                          scope_type=b["scope_type"], scope_id=b.get("scope_id"),
                          granted_by=inv.invited_by, reason=f"invitation {inv.id}")
    for gid in inv.group_ids:
        db.execute(text("INSERT INTO identity_group_members (group_id, principal_id, source, added_by) "
                        "VALUES (:g, :p, 'manual', :by) ON CONFLICT DO NOTHING"),
                   {"g": gid, "p": pid, "by": inv.invited_by})
    db.execute(text("UPDATE identity_invitations SET accepted_principal_id = :p WHERE id = :id"),
               {"p": pid, "id": inv.id})
    for b in inv.bindings:
        emit(db, org_id=inv.org_id, event_type="role_binding.added", outcome=Outcome.SUCCESS,
             actor_principal_id=inv.invited_by, target_type="user", target_id=pid,
             details={"role_key": b["role_key"], "scope": b["scope_type"], "via": "invitation"})
        if b["role_key"] == "owner":
            emit(db, org_id=inv.org_id, event_type="owner.added", outcome=Outcome.SUCCESS,
                 actor_principal_id=inv.invited_by, target_type="user", target_id=pid)
    from .authn_support import bump_epoch

    try:
        bump_epoch(inv.org_id, db=db, reason="invitation accepted")
    except NotImplementedError:
        pass
    return pid
