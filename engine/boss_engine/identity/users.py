"""People: principal + user rows, lookups, status, MFA requirement and factor inventory
(IDENTITY.md §3, §4). Every query is scoped by org; identity comes from rows, never from a body.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional

from sqlalchemy import text

from . import ids
from .authn_support import get_policy
from .principal import Principal, PrincipalKind, PrincipalStatus

#: PATCH allowlist for a person (mass assignment, §6.11): anything else is a 422.
UPDATABLE_FIELDS = frozenset({"display_name", "email"})


@dataclass(frozen=True)
class UserRow:
    principal_id: str
    org_id: str
    status: str
    display_name: str
    email: str
    email_verified_at: Optional[datetime]
    password_hash: Optional[str]
    mfa_required: bool
    network_bound: bool
    self_signed_up: bool
    last_login_at: Optional[datetime]
    legacy_member_ids: tuple[str, ...] = ()
    avatar_seed: Optional[str] = None

    def principal(self) -> Principal:
        return Principal(self.principal_id, PrincipalKind.USER, self.org_id,
                         PrincipalStatus(self.status), self.display_name, self.network_bound,
                         tuple(self.legacy_member_ids or ()))


_USER_SELECT = (
    "SELECT p.id AS principal_id, p.org_id, p.status, p.display_name, p.network_bound, "
    "p.legacy_member_ids, u.email, u.email_verified_at, u.password_hash, u.mfa_required, "
    "u.self_signed_up, u.last_login_at, u.avatar_seed FROM identity_principals p "
    "JOIN identity_users u ON u.principal_id = p.id ")


def _row(r: Any) -> UserRow:
    return UserRow(r.principal_id, r.org_id, r.status, r.display_name, r.email,
                   r.email_verified_at, r.password_hash, bool(r.mfa_required),
                   bool(r.network_bound), bool(r.self_signed_up), r.last_login_at,
                   tuple(r.legacy_member_ids or ()), getattr(r, "avatar_seed", None))


def by_email(db: Any, email: str) -> Optional[UserRow]:
    r = db.execute(text(_USER_SELECT + "WHERE lower(u.email) = lower(:e) AND p.kind = 'user'"),
                   {"e": (email or "").strip()}).first()
    return _row(r) if r else None


def by_id(db: Any, principal_id: str, *, org_id: Optional[str] = None) -> Optional[UserRow]:
    sql = _USER_SELECT + "WHERE p.id = :id AND p.kind = 'user'"
    params: dict[str, Any] = {"id": principal_id}
    if org_id:
        sql += " AND p.org_id = :org"
        params["org"] = org_id
    r = db.execute(text(sql), params).first()
    return _row(r) if r else None


def load_principal(db: Any, principal_id: str) -> Optional[Principal]:
    """Any principal kind, live from its row (status is always checked live)."""
    r = db.execute(text("SELECT id, kind, org_id, status, display_name, network_bound, "
                        "legacy_member_ids FROM identity_principals WHERE id = :id"),
                   {"id": principal_id}).first()
    if r is None:
        return None
    return Principal(r.id, PrincipalKind(r.kind), r.org_id, PrincipalStatus(r.status),
                     r.display_name, bool(r.network_bound), tuple(r.legacy_member_ids or ()))


def count_people(db: Any, org_id: Optional[str] = None) -> int:
    """Real people: users that are not the network-bridge or legacy placeholders."""
    sql = ("SELECT count(*) FROM identity_principals p JOIN identity_users u ON u.principal_id = p.id "
           "WHERE p.kind = 'user' AND NOT p.network_bound AND p.status <> 'pending'")
    params: dict[str, Any] = {}
    if org_id:
        sql += " AND p.org_id = :org"
        params["org"] = org_id
    return int(db.execute(text(sql), params).scalar() or 0)


def create_user(db: Any, *, org_id: str, email: str, display_name: str,
                password_hash: Optional[str] = None, status: str = "active",
                email_verified: bool = False, created_by: Optional[str] = None,
                self_signed_up: bool = False, principal_id: Optional[str] = None) -> str:
    pid = principal_id or ids.new_id("usr")
    db.execute(text(
        "INSERT INTO identity_principals (id, org_id, kind, status, display_name, created_by) "
        "VALUES (:id, :org, 'user', :st, :name, :by)"),
        {"id": pid, "org": org_id, "st": status, "name": display_name.strip()[:200] or email,
         "by": created_by})
    db.execute(text(
        "INSERT INTO identity_users (principal_id, email, email_verified_at, password_hash, "
        "password_changed_at, self_signed_up) VALUES (:id, :email, "
        "CASE WHEN CAST(:ver AS boolean) THEN now() ELSE NULL END, CAST(:ph AS text), "
        "CASE WHEN CAST(:ph AS text) IS NULL THEN NULL ELSE now() END, :ssu)"),
        {"id": pid, "email": email.strip(), "ver": email_verified, "ph": password_hash,
         "ssu": self_signed_up})
    return pid


def set_password_hash(db: Any, principal_id: str, phc: str) -> None:
    db.execute(text("UPDATE identity_users SET password_hash = :ph, password_changed_at = now() "
                    "WHERE principal_id = :id"), {"ph": phc, "id": principal_id})


def rehash_quietly(db: Any, principal_id: str, phc: str) -> None:
    """Upgrade a weak stored hash without counting it as a password change."""
    db.execute(text("UPDATE identity_users SET password_hash = :ph WHERE principal_id = :id"),
               {"ph": phc, "id": principal_id})


def mark_login(db: Any, principal_id: str) -> None:
    db.execute(text("UPDATE identity_users SET last_login_at = now() WHERE principal_id = :id"),
               {"id": principal_id})


def change_email(db: Any, user: UserRow, new_email: str, *, by: str) -> bool:
    """THE way a person's email changes (admin PATCH, SCIM). M2: the new address is UNVERIFIED
    (no mail sign-in, reset or SSO invitation reaches it until it is proven), pending reset links
    to the old one are voided, and the OLD address is told, so a takeover by email change is
    visible to the person it is aimed at. Returns False when nothing changed."""
    new_email = (new_email or "").strip()
    if new_email.lower() == (user.email or "").lower():
        return False
    db.execute(text("UPDATE identity_users SET email = :e, email_verified_at = NULL WHERE principal_id = :id"),
               {"e": new_email, "id": user.principal_id})
    from . import recovery
    from .authn_support import send_mail, smtp_configured

    recovery.void_reset_tokens(db, user.principal_id)
    if smtp_configured() and user.email:
        send_mail(user.email, "Your BOSS email address was changed",
                  "The email address on your BOSS account was just changed (by "
                  f"{'your identity provider' if by.startswith('sys_scim_') else 'an administrator'}). "
                  "If you did not expect this, contact an administrator now.")
    return True


def set_status(db: Any, principal_id: str, status: str, *, by: Optional[str],
               reason: Optional[str] = None) -> None:
    db.execute(text("UPDATE identity_principals SET status = :st, status_changed_at = now(), "
                    "status_changed_by = :by, status_reason = :r, updated_at = now() WHERE id = :id"),
               {"st": status, "by": by, "r": reason, "id": principal_id})


# ---- access roles held (for MFA policy and /me) ---------------------------------------------------

def role_keys(db: Any, principal_id: str, *, org_scope_only: bool = False) -> list[str]:
    """Access-role keys bound to the principal directly or through its groups (unexpired)."""
    sql = ("SELECT DISTINCT r.key FROM identity_role_bindings b JOIN identity_access_roles r "
           "ON r.id = b.role_id WHERE (b.expires_at IS NULL OR b.expires_at > now()) AND ("
           "(b.subject_type = 'principal' AND b.subject_id = :p) OR (b.subject_type = 'group' "
           "AND b.subject_id IN (SELECT group_id FROM identity_group_members WHERE principal_id = :p)))")
    if org_scope_only:
        sql += " AND b.scope_type = 'org'"
    return sorted(r[0] for r in db.execute(text(sql + " ORDER BY 1"), {"p": principal_id}))


def mfa_required(db: Any, user: UserRow) -> bool:
    """MFA is required when the user's own flag says so, or any access role they hold is in the
    policy's MFA-required list or is marked mfa_required (Owner, Admin, Auditor by default)."""
    if user.mfa_required:
        return True
    required = set(get_policy(user.org_id, db=db).mfa_required_roles)
    held = role_keys(db, user.principal_id)
    if required.intersection(held):
        return True
    r = db.execute(text(
        "SELECT 1 FROM identity_role_bindings b JOIN identity_access_roles r ON r.id = b.role_id "
        "WHERE r.mfa_required AND (b.expires_at IS NULL OR b.expires_at > now()) AND ("
        "(b.subject_type = 'principal' AND b.subject_id = :p) OR (b.subject_type = 'group' AND "
        "b.subject_id IN (SELECT group_id FROM identity_group_members WHERE principal_id = :p))) LIMIT 1"),
        {"p": user.principal_id}).first()
    return r is not None


@dataclass(frozen=True)
class Factors:
    totp: bool
    passkeys: int
    recovery_codes_remaining: int

    @property
    def any_second_factor(self) -> bool:
        return self.totp or self.passkeys > 0


def factors(db: Any, principal_id: str) -> Factors:
    r = db.execute(text(
        "SELECT (SELECT count(*) FROM identity_totp WHERE principal_id = :p) AS totp, "
        "(SELECT count(*) FROM identity_webauthn_credentials WHERE principal_id = :p) AS pk, "
        "(SELECT count(*) FROM identity_recovery_codes WHERE principal_id = :p AND used_at IS NULL) AS rc"),
        {"p": principal_id}).first()
    return Factors(bool(r.totp), int(r.pk), int(r.rc))


def add_binding(db: Any, *, org_id: str, subject_id: str, role_key: str, scope_type: str = "org",
                scope_id: Optional[str] = None, granted_by: str, subject_type: str = "principal",
                reason: Optional[str] = None) -> str:
    """Insert a role binding for a built-in role (claim, invitation acceptance). The subset and
    owner:assign rules are the CALLER's checks; this only writes."""
    rid = db.execute(text("SELECT id FROM identity_access_roles WHERE key = :k AND (org_id IS NULL OR org_id = :org) "
                          "ORDER BY org_id NULLS FIRST LIMIT 1"), {"k": role_key, "org": org_id}).scalar()
    if rid is None:
        raise ValueError(f"unknown access role {role_key!r}")
    bid = ids.new_id("rbd")
    db.execute(text(
        "INSERT INTO identity_role_bindings (id, org_id, subject_type, subject_id, role_id, scope_type, "
        "scope_id, granted_by, reason) VALUES (:id, :org, :st, :sid, :rid, :sct, :scid, :by, :r) "
        "ON CONFLICT (subject_type, subject_id, role_id, scope_type, scope_id) DO NOTHING"),
        {"id": bid, "org": org_id, "st": subject_type, "sid": subject_id, "rid": rid,
         "sct": scope_type, "scid": scope_id or org_id, "by": granted_by, "r": reason})
    return bid


def active_owner_ids(db: Any, org_id: str) -> list[str]:
    """Who is an Owner right now (bindings.owner_ids: one definition of Owner reach)."""
    from .bindings import owner_ids

    return sorted(owner_ids(db, org_id))


def is_owner(db: Any, principal_id: str, org_id: str) -> bool:
    return principal_id in active_owner_ids(db, org_id)


def strip_unknown(fields: Iterable[str], allowed: frozenset[str]) -> list[str]:
    """The keys of a PATCH body that are not allowed (the caller answers 422 when any)."""
    return sorted(set(fields) - set(allowed))


def factor_list(db: Any, principal_id: str) -> list[dict[str, Any]]:
    """Every sign-in factor a person holds, for the console (types.ts `Factor`). Never a secret:
    no TOTP secret, no credential id, no subject."""
    out: list[dict[str, Any]] = []
    for r in db.execute(text("SELECT id, name, last_used_at, backup_state FROM identity_webauthn_credentials "
                             "WHERE principal_id = :p ORDER BY created_at"), {"p": principal_id}):
        out.append({"id": r.id, "kind": "passkey", "name": r.name, "detail": "Passkey",
                    "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
                    "note": "synced" if r.backup_state else None})
    t = db.execute(text("SELECT enrolled_at FROM identity_totp WHERE principal_id = :p"), {"p": principal_id}).first()
    if t:
        out.append({"id": "totp", "kind": "totp", "name": "Authenticator app", "detail": "6-digit codes",
                    "last_used_at": None, "note": f"added {t.enrolled_at.date().isoformat()}"})
    f = factors(db, principal_id)
    total = int(db.execute(text("SELECT count(*) FROM identity_recovery_codes WHERE principal_id = :p"),
                           {"p": principal_id}).scalar() or 0)
    if total:
        out.append({"id": "recovery", "kind": "recovery", "name": "Recovery codes",
                    "detail": f"{f.recovery_codes_remaining} of {total} left", "last_used_at": None,
                    "note": "regenerate soon" if f.recovery_codes_remaining < 3 else None})
    for r in db.execute(text(
            "SELECT i.id, coalesce(p.name, i.provider_id) AS provider, i.last_used_at FROM identity_user_identities i "
            "LEFT JOIN identity_providers p ON p.id = i.provider_id WHERE i.principal_id = :p ORDER BY i.linked_at"),
            {"p": principal_id}):
        out.append({"id": r.id, "kind": "sso", "name": r.provider, "detail": "Single sign-on",
                    "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None, "note": None})
    return out
