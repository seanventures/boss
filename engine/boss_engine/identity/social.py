"""Turning a verified federated identity into a BOSS person (IDENTITY.md §4.8 "Linking", §4.15;
D31). Stream S1. Shared by OIDC, GitHub and SAML.

The ONLY key is (provider, issuer, subject) (M1: a second provider that names the same issuer can
never sign in as the first one's people; issuers are also unique per org). In order:
  1. already linked                 -> that person, if active;
  2. purpose=link                   -> bind to the signed-in, stepped-up person who started it;
  3. an open, unclaimed invitation for the VERIFIED email -> accept it, link;
  4. the provider's JIT domain rule -> create the person with NO access roles, link;
  5. self sign-up on and the domain allowed -> create a self-signed-up person, no roles, link;
  otherwise refused: "Ask an admin to invite you." An email that already belongs to a BOSS account
  is NEVER linked or signed in by email, whatever `email_verified` says (A61, A89, A90).
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text

from . import invitations, users
from .audit import Outcome
from .bindings import guard_membership_removals
from .roles import AccessError
from .authn_support import (
    bump_epoch,
    emit,
    get_policy,
    identifier_hash,
    random_token,
    sha256_hex,
)
from .signin import Answer, RequestFacts, finish_first_factor
from .sso_oidc import FederatedIdentity, Provider

NOT_INVITED = {"error": "not_invited", "message": "Ask an admin to invite you."}


class Refused(Exception):
    def __init__(self, code: str, message: Optional[str] = None):
        super().__init__(code)
        self.code = code
        self.message = message


def _domain(email: Optional[str]) -> str:
    return email.rsplit("@", 1)[1].lower() if email and "@" in email else ""


def linked_principal(db: Any, ident: FederatedIdentity, provider: Provider) -> Optional[str]:
    """The person linked to this subject AT THIS PROVIDER (M1). A link made through another
    provider never matches, even when that provider claims the same issuer."""
    return db.execute(text("SELECT principal_id FROM identity_user_identities WHERE provider_id = :p "
                           "AND issuer = :i AND subject = :s"),
                      {"p": provider.id, "i": ident.issuer, "s": ident.subject}).scalar()


def vouches_for(provider: Provider, ident: FederatedIdentity) -> bool:
    """Does this provider's word on the email count (M1)? Only a VERIFIED email in a domain the
    admin listed on the provider (its `domains`: the provider is authoritative there). A SAML
    IdP's or a social login's say-so about any other domain proves nothing."""
    email = (ident.email or "").strip()
    trusted = {d.lower().lstrip("@") for d in (provider.domains or ())}
    return bool(email and ident.email_verified and _domain(email) in trusted)


def link(db: Any, *, principal_id: str, provider: Provider, ident: FederatedIdentity) -> str:
    lid = "lnk_" + sha256_hex(random_token())[:26]
    db.execute(text("INSERT INTO identity_user_identities (id, principal_id, provider_id, issuer, subject, "
                    "email_at_link, last_used_at) VALUES (:id, :p, :pr, :i, :s, :e, now())"),
               {"id": lid, "p": principal_id, "pr": provider.id, "i": ident.issuer, "s": ident.subject,
                "e": (ident.email or None)})
    emit(db, org_id=provider.org_id, event_type="sso.link", outcome=Outcome.SUCCESS,
         actor_principal_id=principal_id, actor_kind="user", target_type="identity", target_id=lid,
         details={"provider_id": provider.id, "issuer": ident.issuer})
    return lid


def sync_groups(db: Any, *, principal_id: str, provider: Provider, ident: FederatedIdentity) -> None:
    """Mapped IdP groups become memberships (source=oidc); an unmapped group grants nothing; a
    mapped group the IdP no longer sends is removed. Only this provider's mapped groups are touched."""
    if not provider.groups_claim:
        return
    mappings = {r.external_group: r.group_id for r in db.execute(text(
        "SELECT external_group, group_id FROM identity_provider_group_mappings WHERE provider_id = :p"),
        {"p": provider.id})}
    if not mappings:
        return
    wanted = {mappings[g] for g in ident.groups if g in mappings}
    managed = set(mappings.values())
    current = {r[0] for r in db.execute(text(
        "SELECT group_id FROM identity_group_members WHERE principal_id = :p AND source = 'oidc' "
        "AND group_id = ANY(:g)"), {"p": principal_id, "g": sorted(managed)})}
    changed = False
    for gid in sorted(wanted - current):
        db.execute(text("INSERT INTO identity_group_members (group_id, principal_id, source, added_by) "
                        "VALUES (:g, :p, 'oidc', :by) ON CONFLICT (group_id, principal_id) DO NOTHING"),
                   {"g": gid, "p": principal_id, "by": f"sso:{provider.id}"})
        emit(db, org_id=provider.org_id, event_type="group.member_added", outcome=Outcome.SUCCESS,
             actor_principal_id=f"sso:{provider.id}", target_type="group", target_id=gid,
             details={"member": principal_id, "source": "oidc"})
        changed = True
    for gid in sorted(current - wanted):
        try:
            # H4: the IdP dropping a group must not remove the last Owner's reach (the deferred
            # trigger would otherwise fail the whole sign-in at commit)
            guard_membership_removals(db, provider.org_id, [(gid, principal_id)])
        except AccessError:
            emit(db, org_id=provider.org_id, event_type="group.member_removed", outcome=Outcome.DENIED,
                 actor_principal_id=f"sso:{provider.id}", target_type="group", target_id=gid,
                 details={"member": principal_id, "source": "oidc", "reason": "last_owner"})
            continue
        db.execute(text("DELETE FROM identity_group_members WHERE group_id = :g AND principal_id = :p "
                        "AND source = 'oidc'"), {"g": gid, "p": principal_id})
        emit(db, org_id=provider.org_id, event_type="group.member_removed", outcome=Outcome.SUCCESS,
             actor_principal_id=f"sso:{provider.id}", target_type="group", target_id=gid,
             details={"member": principal_id, "source": "oidc"})
        changed = True
    if changed:
        try:
            bump_epoch(provider.org_id, db=db, reason="sso group sync")
        except NotImplementedError:
            pass


def _refuse(db: Any, provider: Provider, facts: RequestFacts, ident: FederatedIdentity, reason: str) -> None:
    emit(db, org_id=provider.org_id, event_type="auth.login.failed", outcome=Outcome.DENIED,
         ip=facts.ip, user_agent=facts.user_agent, request_id=facts.request_id,
         details={"reason": reason, "provider_id": provider.id, "issuer": ident.issuer,
                  "identifier": identifier_hash(ident.email or "")})


def _create(db: Any, provider: Provider, ident: FederatedIdentity, *, self_signed_up: bool) -> str:
    return users.create_user(db, org_id=provider.org_id, email=ident.email or "",
                             display_name=ident.name or ident.email or "", password_hash=None,
                             email_verified=True, created_by=f"sso:{provider.id}",
                             self_signed_up=self_signed_up)


def _provisioned_here(db: Any, provider: Provider, principal_id: str) -> bool:
    """Did THIS provider's SCIM connection create the account, and is it linked to no identity of
    this provider yet (so an existing link is never replaced)?"""
    from .scim import provisioning_principal

    row = db.execute(text(
        "SELECT p.created_by, p.org_id, EXISTS (SELECT 1 FROM identity_user_identities i "
        "WHERE i.principal_id = p.id AND i.provider_id = :prov) AS linked "
        "FROM identity_principals p WHERE p.id = :id AND p.kind = 'user'"),
        {"id": principal_id, "prov": provider.id}).mappings().first()
    return bool(row and row["org_id"] == provider.org_id and not row["linked"]
                and row["created_by"] == provisioning_principal(provider.id))


def resolve(db: Any, provider: Provider, ident: FederatedIdentity, facts: RequestFacts, *,
            purpose: str = "login", link_principal: Optional[str] = None) -> Answer:
    """Sign in (or link) a verified federated identity. Returns the sign-in Answer, or a refusal
    Answer whose body names only a safe code."""
    if not ident.issuer or not ident.subject:
        return Answer(401, {"error": "sso_failed"})
    existing = linked_principal(db, ident, provider)
    if existing is None and purpose != "link" and db.execute(text(
            "SELECT 1 FROM identity_user_identities WHERE issuer = :i AND subject = :s AND provider_id <> :p"),
            {"i": ident.issuer, "s": ident.subject, "p": provider.id}).first() is not None:
        # The subject is linked through ANOTHER provider with the same issuer: never this one's.
        _refuse(db, provider, facts, ident, "issuer_collision")
        return Answer(403, dict(NOT_INVITED))
    if purpose == "link":
        from . import sessions

        row = sessions.lookup(db, facts.session)
        if not link_principal or row is None or row.principal_id != link_principal:
            _refuse(db, provider, facts, ident, "link_session_mismatch")
            return Answer(401, {"error": "link_invalid"})
        if existing and existing != link_principal:
            _refuse(db, provider, facts, ident, "already_linked")
            return Answer(409, {"error": "already_linked"})
        if not existing:
            link(db, principal_id=link_principal, provider=provider, ident=ident)
        return Answer(200, {"next": "linked"})
    if existing:
        user = users.by_id(db, existing)
        if user is None:
            return Answer(401, {"error": "sso_failed"})
        if user.status == "disabled":
            _refuse(db, provider, facts, ident, "disabled")
            return Answer(403, {"error": "account_disabled", "message": "This account is disabled. Ask an admin."})
        if user.status == "pending":
            users.set_status(db, user.principal_id, "active", by=f"sso:{provider.id}", reason="first SSO sign-in")
            user = users.by_id(db, existing)
        if user is None or user.status != "active":
            return Answer(401, {"error": "sso_failed"})
        db.execute(text("UPDATE identity_user_identities SET last_used_at = now() WHERE provider_id = :p "
                        "AND issuer = :i AND subject = :s"),
                   {"p": provider.id, "i": ident.issuer, "s": ident.subject})
        return _sign_in(db, provider, ident, user, facts)
    # Unknown subject: only an invitation, a JIT rule or enabled self sign-up may create someone,
    # and only for a VERIFIED email that no BOSS account holds yet.
    email = (ident.email or "").strip()
    if not email or not ident.email_verified:
        _refuse(db, provider, facts, ident, "unverified_email")
        return Answer(403, dict(NOT_INVITED))
    holder = users.by_email(db, email)
    if holder is not None and _provisioned_here(db, provider, holder.principal_id):
        # THIS provider's SCIM connection made the account (R2 #11): the IdP vouched for the
        # person twice — by provisioning them and by this verified sign-in — so the first SSO
        # sign-in links it (once; a subject already linked here never re-links) and activates a
        # pending one. A disabled one is refused with words.
        if holder.status == "disabled":
            _refuse(db, provider, facts, ident, "disabled")
            return Answer(403, {"error": "account_disabled", "message": "This account is disabled. Ask an admin."})
        link(db, principal_id=holder.principal_id, provider=provider, ident=ident)
        if holder.status == "pending":
            users.set_status(db, holder.principal_id, "active", by=f"sso:{provider.id}",
                             reason="first SSO sign-in (SCIM-provisioned)")
        user = users.by_id(db, holder.principal_id)
        if user is None or user.status != "active":
            return Answer(401, {"error": "sso_failed"})
        return _sign_in(db, provider, ident, user, facts)
    if holder is not None:
        _refuse(db, provider, facts, ident, "email_belongs_to_unlinked_account")
        return Answer(403, dict(NOT_INVITED))
    inv = invitations.open_for_email(db, email)
    pid: Optional[str] = None
    if inv is not None and inv.org_id == provider.org_id and vouches_for(provider, ident):
        # M1: an invitation (it may name an Owner) is claimed by SSO only when this provider is
        # authoritative for the email's domain; otherwise the person accepts it with the link.
        try:
            pid = invitations.accept(db, inv, display_name=ident.name or email, password_hash=None,
                                     email_verified=True)
        except invitations.InvitationRefused:
            # the inviter can no longer grant what the invitation names (C1): nothing was written
            _refuse(db, provider, facts, ident, "invitation_invalid")
            return Answer(403, dict(NOT_INVITED))
    elif provider.jit_mode == "domain" and _domain(email) in {d.lower().lstrip("@") for d in provider.domains}:
        pid = _create(db, provider, ident, self_signed_up=False)
    else:
        pol = get_policy(provider.org_id, db=db)
        domains = {d.lower().lstrip("@") for d in pol.signup_domains}
        if pol.signup_enabled and (not domains or _domain(email) in domains):
            pid = _create(db, provider, ident, self_signed_up=True)
            emit(db, org_id=provider.org_id, event_type="auth.signup.completed", outcome=Outcome.SUCCESS,
                 target_type="user", target_id=pid, ip=facts.ip, details={"via": f"sso:{provider.id}"})
    if pid is None:
        _refuse(db, provider, facts, ident, "not_invited")
        return Answer(403, dict(NOT_INVITED))
    link(db, principal_id=pid, provider=provider, ident=ident)
    user = users.by_id(db, pid)
    assert user is not None
    return _sign_in(db, provider, ident, user, facts)


def _sign_in(db: Any, provider: Provider, ident: FederatedIdentity, user: users.UserRow,
             facts: RequestFacts) -> Answer:
    sync_groups(db, principal_id=user.principal_id, provider=provider, ident=ident)
    emit(db, org_id=provider.org_id, event_type="sso.login", outcome=Outcome.SUCCESS,
         actor_principal_id=user.principal_id, actor_kind="user", ip=facts.ip,
         user_agent=facts.user_agent, request_id=facts.request_id,
         details={"provider_id": provider.id, "issuer": ident.issuer, "idp_mfa": ident.mfa})
    return finish_first_factor(db, user, facts, method=f"sso:{provider.id}", idp_mfa=ident.mfa)
