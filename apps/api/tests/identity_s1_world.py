"""Stream S1's test world: fakes of the OTHER streams' frozen interfaces, and helpers that drive
the real sign-in API through the real app (IDENTITY_BUILD.md §3: "until the other side lands, each
stream tests against a fake of the frozen interface").

Fakes (each is the smallest faithful implementation of its frozen docstring):
  audit.record            -> appends to WORLD.events and writes a real identity_security_events
                             row (so `bootstrap.claimed` arms the last-Owner trigger); refuses
                             unknown event types and secret-looking detail keys
  policy.get_policy       -> identity_settings.policy over the IdentityPolicy defaults
  vault.store/get_credential -> an in-memory store (ciphertext never needed by S1's tests)
  authz.effective_permissions / check / bump_epoch -> role bindings x the generated catalogue,
                             key scopes intersected, step-up from session.mfa_at (§6.6)
  legacy.bridge           -> None, or the network operator when a test says so
  delegation.revoke_cascade -> recorded

Never import this from product code.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Optional

import pytest
from sqlalchemy import text

from boss_engine.identity import audit, authz, delegation, legacy, policy, vault
from boss_engine.identity import authn_support, ids, signin, email_signin
from boss_engine.identity.authz import Decision, DenyReason, EffectivePermissions
from boss_engine.identity.catalogue import meta
from boss_engine.identity.permissions_gen import ROLE_PERMISSIONS
from boss_engine.identity.principal import AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus
from boss_engine.identity.scope import ResolvedScope, ScopeKind

ORIGIN = "https://boss.example.test"
HEADERS = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
STRONG = "violet-otter-harbour-1987"
SECRET_KEY_RE = re.compile(r"token|secret|password|code|assertion|credential", re.I)
IDENTITY_TABLES_CHILD_FIRST = [
    "identity_recovery_codes", "identity_totp", "identity_webauthn_credentials",
    "identity_user_identities", "identity_provider_group_mappings", "identity_providers",
    "identity_auth_challenges", "identity_sessions", "identity_invitations", "identity_group_members",
    "identity_groups", "identity_role_bindings", "identity_api_credentials",
    "identity_service_accounts", "identity_rate_limits", "identity_legacy_mappings",
    "identity_settings", "identity_approval_seats", "identity_users",
]


@dataclass
class World:
    events: list[audit.SecurityEvent] = field(default_factory=list)
    vault: dict[str, str] = field(default_factory=dict)
    mail: list[tuple[str, str, str]] = field(default_factory=list)
    cascades: list[tuple[str, str]] = field(default_factory=list)
    network_bridge: bool = False
    smtp: bool = False

    def events_of(self, event_type: str) -> list[audit.SecurityEvent]:
        return [e for e in self.events if e.event_type == event_type]

    def last_mail_to(self, address: str) -> tuple[str, str]:
        for to, subject, body in reversed(self.mail):
            if to.lower() == address.lower():
                return subject, body
        raise AssertionError(f"no mail to {address}")


# ---- fakes ---------------------------------------------------------------------------------------

def _fake_record(world: World):
    def record(event: audit.SecurityEvent, *, db: Any) -> audit.RecordedEvent:
        if event.event_type not in audit.EVENT_TYPES:
            raise ValueError(f"unknown event type {event.event_type}")
        for key in (event.details or {}):
            if SECRET_KEY_RE.search(key):
                raise ValueError(f"details key {key!r} looks like a secret")
        seq = int(db.execute(text("SELECT coalesce(max(seq), 0) + 1 FROM identity_security_events "
                                  "WHERE org_id = :o"), {"o": event.org_id}).scalar())
        row = db.execute(text(
            "INSERT INTO identity_security_events (org_id, seq, stream, event_type, outcome, "
            "actor_principal_id, actor_kind, target_type, target_id, details, prev_hash, hash, session_ref, "
            "credential_id) VALUES (:o, :s, :st, :et, :oc, :a, :ak, :tt, :ti, CAST(:d AS jsonb), :ph, :h, :sr, :cr) "
            "RETURNING id"),
            {"o": event.org_id, "s": seq, "st": event.stream.value, "et": event.event_type,
             "oc": event.outcome.value, "a": event.actor_principal_id, "ak": event.actor_kind,
             "tt": event.target_type, "ti": event.target_id,
             "d": json.dumps(dict(event.details or {}), default=str), "ph": b"\0" * 32,
             "h": hashlib.sha256(f"{event.org_id}{seq}".encode()).digest(),
             "sr": event.session_ref, "cr": event.credential_id}).scalar()
        world.events.append(event)
        return audit.RecordedEvent(int(row), seq, b"")
    return record


def _fake_get_policy(org_id: str, *, db: Any) -> policy.IdentityPolicy:
    raw = db.execute(text("SELECT policy FROM identity_settings WHERE org_id = :o"), {"o": org_id}).scalar()
    data = raw if isinstance(raw, dict) else json.loads(raw or "{}")
    known = {k: v for k, v in data.items() if k in policy.IdentityPolicy.__dataclass_fields__}
    for k in ("mfa_required_roles", "signup_domains"):
        if k in known:
            known[k] = tuple(known[k])
    return policy.IdentityPolicy(**known)


def _fake_vault(world: World):
    def store(*, org_id, owner_principal_id, kind, provider, secret, refresh=None, access_expires_at=None, db):
        item = ids.new_id("vlt")
        world.vault[item] = secret
        return item

    def get_credential(connection, purpose, *, ctx, task=None, db):
        secret = world.vault[connection.item_id]
        return vault.ShortLivedCredential(token=secret, provider=connection.provider, item_id=connection.item_id)
    return store, get_credential


def role_permissions_at(db: Any, principal_id: str, scope: ResolvedScope) -> set[str]:
    rows = db.execute(text(
        "SELECT r.key, b.scope_type, b.scope_id FROM identity_role_bindings b JOIN identity_access_roles r "
        "ON r.id = b.role_id WHERE (b.expires_at IS NULL OR b.expires_at > now()) AND ("
        "(b.subject_type = 'principal' AND b.subject_id = :p) OR (b.subject_type = 'group' AND b.subject_id IN "
        "(SELECT group_id FROM identity_group_members WHERE principal_id = :p)))"), {"p": principal_id})
    held: set[str] = set()
    for key, scope_type, scope_id in rows:
        perms = set(ROLE_PERMISSIONS.get(key, frozenset()))
        if scope_type == "org":
            held |= perms
        elif scope.kind == ScopeKind.PROJECT and scope_id == scope.project_id:
            held |= {p for p in perms if not meta(p).org_only}
    return held


def _fake_effective(subject, scope, *, db=None, with_why=False):
    ctx = subject if isinstance(subject, AuthContext) else None
    principal = ctx.principal if ctx else subject
    perms = role_permissions_at(db, principal.id, scope)
    if principal.kind == PrincipalKind.SYSTEM and principal.id.startswith("sys_scim_"):
        perms = set(ROLE_PERMISSIONS["system_scim"])
    if ctx and ctx.credential:
        perms &= set(ctx.credential.scopes)
    epoch = int(db.execute(text("SELECT coalesce(max(epoch), 0) FROM identity_settings WHERE org_id = :o"),
                           {"o": principal.org_id}).scalar() or 0)
    fresh = None
    if ctx and ctx.session and ctx.session.mfa_at:
        fresh = ctx.session.mfa_at + timedelta(minutes=10)
    return EffectivePermissions(frozenset(perms), scope, epoch, fresh)


def _fake_check(ctx, permission, scope, *, db=None):
    from boss_engine.db import begin

    if db is None:
        with begin() as conn:
            return _fake_check(ctx, permission, scope, db=conn)
    m = meta(permission)
    eff = _fake_effective(ctx, scope, db=db)
    if permission not in eff.permissions:
        return Decision.deny(permission, scope, DenyReason.MISSING_PERMISSION)
    if m.step_up:
        s = ctx.session
        if s is None or s.mfa_at is None or (authn_support.utcnow() - s.mfa_at).total_seconds() > 600:
            return Decision.deny(permission, scope, DenyReason.STEP_UP_REQUIRED)
    return Decision.allow(permission, scope)


def _fake_bump(org_id, *, db, reason):
    db.execute(text("INSERT INTO identity_settings (org_id, policy, epoch) VALUES (:o, '{}', 1) "
                    "ON CONFLICT (org_id) DO UPDATE SET epoch = identity_settings.epoch + 1"), {"o": org_id})
    return 0


def _fake_bridge(world: World):
    def bridge(creds, *, db=None):
        if world.network_bridge and creds.legacy_auth_method == "network":
            from boss_engine.identity.users import load_principal
            from boss_engine.db import begin

            with begin() as conn:
                p = load_principal(conn, legacy.NETWORK_OPERATOR_PRINCIPAL)
            return AuthContext(p, AuthMethod.NETWORK) if p else None
        return None
    return bridge


def _fake_cascade(world: World):
    def revoke_cascade(trigger, target_id, *, actor, reason, db, dry_run=False):
        world.cascades.append((str(trigger), target_id))
        return None
    return revoke_cascade


# ---- the fixture ------------------------------------------------------------------------------------

def wipe(db: Any) -> None:
    for table in IDENTITY_TABLES_CHILD_FIRST:
        db.execute(text(f"DELETE FROM {table}"))
    db.execute(text("SET LOCAL boss.identity_audit_prune = 'on'"))
    db.execute(text("DELETE FROM identity_security_events"))
    db.execute(text("DELETE FROM identity_principals WHERE kind <> 'agent'"))


def install(monkeypatch, world: World) -> None:
    from boss_engine.config import get_settings

    monkeypatch.setattr(audit, "record", _fake_record(world))
    monkeypatch.setattr(policy, "get_policy", _fake_get_policy)
    store, get_credential = _fake_vault(world)
    monkeypatch.setattr(vault, "store", store)
    monkeypatch.setattr(vault, "get_credential", get_credential)
    monkeypatch.setattr(authz, "effective_permissions", _fake_effective)
    monkeypatch.setattr(authz, "check", _fake_check)
    monkeypatch.setattr(authz, "bump_epoch", _fake_bump)
    monkeypatch.setattr(legacy, "bridge", _fake_bridge(world))
    monkeypatch.setattr(delegation, "revoke_cascade", _fake_cascade(world))
    settings = get_settings()
    monkeypatch.setattr(settings, "public_url", ORIGIN)
    monkeypatch.setattr(settings, "argon2_profile", "small")
    monkeypatch.setattr(settings, "identity_server_key", "")
    monkeypatch.setattr(signin, "DELAY_ENABLED", False)
    monkeypatch.setattr(email_signin, "UNIFORM_FLOOR_SECONDS", 0.05)
    monkeypatch.setattr(authn_support, "MAIL_TRANSPORT", lambda to, s, b: world.mail.append((to, s, b)))
    monkeypatch.setattr(email_signin, "DELIVER", lambda to, s, b: world.mail.append((to, s, b)))
    monkeypatch.setattr(authn_support, "smtp_configured", lambda: world.smtp)
    monkeypatch.setattr(email_signin, "available", lambda: world.smtp)
    # Rebind the names other modules imported directly.
    from boss_engine.identity import users as users_module, sessions as sessions_module

    monkeypatch.setattr(users_module, "get_policy", lambda org, *, db: _fake_get_policy(org, db=db))
    monkeypatch.setattr(sessions_module, "get_policy", lambda org, *, db: _fake_get_policy(org, db=db))
    monkeypatch.setattr(email_signin, "get_policy", lambda org, *, db: _fake_get_policy(org, db=db))
    from boss_engine.identity import api_keys as keys_module

    monkeypatch.setattr(keys_module, "get_policy", lambda org, *, db: _fake_get_policy(org, db=db))


@pytest.fixture
def world(monkeypatch, engine):
    w = World()
    install(monkeypatch, w)
    with engine.begin() as db:
        wipe(db)
    yield w
    with engine.begin() as db:
        wipe(db)


# ---- builders ---------------------------------------------------------------------------------------

def org_id(db: Any) -> str:
    return authn_support.default_org_id(db)


def set_policy(engine, **values: Any) -> None:
    with engine.begin() as db:
        org = org_id(db)
        db.execute(text("INSERT INTO identity_settings (org_id, policy) VALUES (:o, CAST(:p AS jsonb)) "
                        "ON CONFLICT (org_id) DO UPDATE SET policy = identity_settings.policy || CAST(:p AS jsonb)"),
                   {"o": org, "p": json.dumps(values)})


def make_user(engine, email: str, *, password: Optional[str] = STRONG, role: Optional[str] = "operator",
              status: str = "active", name: str = "Test Person", project: Optional[str] = None) -> str:
    from boss_engine.identity import users
    from boss_engine.identity.passwords import hash_password

    with engine.begin() as db:
        org = org_id(db)
        pid = users.create_user(db, org_id=org, email=email, display_name=name,
                                password_hash=hash_password(password) if password else None,
                                status=status, email_verified=True, created_by="test")
        if role:
            users.add_binding(db, org_id=org, subject_id=pid, role_key=role,
                              scope_type="project" if project else "org", scope_id=project,
                              granted_by="test")
    return pid


def mark_claimed(engine) -> None:
    """Record bootstrap.claimed, as the real claim does (arms the last-Owner trigger)."""
    with engine.begin() as db:
        org = org_id(db)
        seq = int(db.execute(text("SELECT coalesce(max(seq),0)+1 FROM identity_security_events WHERE org_id=:o"),
                             {"o": org}).scalar())
        db.execute(text("INSERT INTO identity_security_events (org_id, seq, stream, event_type, outcome, "
                        "prev_hash, hash) VALUES (:o, :s, 'security', 'bootstrap.claimed', 'success', :p, :h)"),
                   {"o": org, "s": seq, "p": b"\0" * 32, "h": b"\1" * 32})


def make_network_operator(engine) -> None:
    with engine.begin() as db:
        db.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name, network_bound) "
                        "VALUES (:id, :o, 'user', 'active', 'Network operator', true) ON CONFLICT (id) DO NOTHING"),
                   {"id": legacy.NETWORK_OPERATOR_PRINCIPAL, "o": org_id(db)})


def sign_in(client, email: str, password: str = STRONG, *, headers: Optional[dict] = None):
    return client.post("/v1/auth/login", json={"email": email, "password": password},
                       headers={**HEADERS, **(headers or {})})


def csrf(client) -> dict[str, str]:
    me = client.get("/v1/identity/me")
    assert me.status_code == 200, me.text
    return {**HEADERS, "X-Boss-CSRF": me.json()["csrf_token"]}


def fresh_client():
    from fastapi.testclient import TestClient
    from apps.api.app.main import app

    return TestClient(app, base_url=ORIGIN)


def totp_now(secret: str, *, offset_steps: int = 0) -> str:
    from boss_engine.identity import totp

    return totp.code_at(secret, totp.current_step() + offset_steps)


def promote_fresh_mfa(engine, session_cookie: str) -> None:
    """Mark a session as freshly stepped-up (for tests whose subject is not step-up itself)."""
    with engine.begin() as db:
        db.execute(text("UPDATE identity_sessions SET mfa_at = now() WHERE token_hash = :h"),
                   {"h": hashlib.sha256(session_cookie.encode()).hexdigest()})


def set_cookie_values(response) -> list[str]:
    return response.headers.get_list("set-cookie")


def principal(pid: str, org: str, kind=PrincipalKind.USER) -> Principal:
    return Principal(pid, kind, org, PrincipalStatus.ACTIVE, pid)


# ---- SCIM and MFA helpers (S1 fork: SCIM + MFA tests) ------------------------------------------------

def make_scim_key(engine, provider: str = "okta") -> str:
    """A sys_scim_<provider> principal and its boss_scim_ key (scopes: scim:provision only)."""
    from boss_engine.identity import api_keys, users

    owner_id = make_user(engine, f"scim-owner-{provider}@example.test", role="owner")
    with engine.begin() as db:
        org = org_id(db)
        pid = f"sys_scim_{provider}"
        db.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) VALUES "
                        "(:id, :o, 'system', 'active', :n) ON CONFLICT (id) DO NOTHING"),
                   {"id": pid, "o": org, "n": f"SCIM ({provider})"})
        owner = users.load_principal(db, owner_id)
        system = users.load_principal(db, pid)
        created = api_keys.create(db, creator=AuthContext(owner, AuthMethod.SESSION), owner=system,
                                  kind="scim", name=f"{provider} SCIM", scopes=["scim:provision"],
                                  check_subset=False)
    return created.key
