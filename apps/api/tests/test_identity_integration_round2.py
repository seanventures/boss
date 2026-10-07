"""Integration round 2 (lead): gaps between the three security fixers' branches."""
from __future__ import annotations

from sqlalchemy import text

from apps.api.tests.identity_s1_world import (  # noqa: F401  (world is a fixture)
    make_user,
    org_id,
    principal,
    world,
)
from boss_engine.identity import AuthContext, AuthMethod, authz, bindings, signin, users

REAL_CHECK = authz.check


def _owner_ctx(engine):
    owner = make_user(engine, "own-r2@example.test", role="owner")
    with engine.begin() as db:
        org = org_id(db)
    from datetime import datetime, timedelta, timezone

    from boss_engine.identity import SessionRef

    now = datetime.now(timezone.utc)
    fresh = SessionRef("ses_r2", now, ("pwd", "totp"), now + timedelta(hours=1), now + timedelta(days=1))
    return AuthContext(principal(owner, org), AuthMethod.SESSION, session=fresh), org


def test_b_promotion_into_an_mfa_required_role_opens_a_first_factor_window(world, engine, monkeypatch):
    """A person with a password who has signed in before, and no second factor, is promoted to
    Admin (MFA required). Without a window every later password-only sign-in is enrolment-less
    (F3/H1) and they are stranded; with it, their next sign-in may enrol a first factor."""
    monkeypatch.setattr(authz, "check", REAL_CHECK)
    ctx, org = _owner_ctx(engine)
    pid = make_user(engine, "promo-r2@example.test", role="operator")
    with engine.begin() as db:
        db.execute(text("UPDATE identity_users SET last_login_at = now() - interval '1 day' WHERE principal_id = :p"),
                   {"p": pid})
        user = users.by_id(db, pid)
        assert signin.first_factor_grant(db, user, mfa=False) is None          # stranded before
        admin = db.execute(text("SELECT id FROM identity_access_roles WHERE key = 'admin' AND org_id IS NULL")).scalar()
        bindings.add_binding(db, ctx, subject_type="principal", subject_id=pid, role_id=admin, scope_type="org")
        user = users.by_id(db, pid)
        assert users.mfa_required(db, user)
        assert signin.first_factor_grant(db, user, mfa=False) is not None      # the window
        assert signin.first_factor_grant(db, user, mfa=False) is None          # consumed once


def test_b_a_person_already_required_gets_no_new_window(world, engine, monkeypatch):
    monkeypatch.setattr(authz, "check", REAL_CHECK)
    ctx, org = _owner_ctx(engine)
    pid = make_user(engine, "aud-r2@example.test", role="auditor")
    with engine.begin() as db:
        db.execute(text("UPDATE identity_users SET last_login_at = now() WHERE principal_id = :p"), {"p": pid})
        admin = db.execute(text("SELECT id FROM identity_access_roles WHERE key = 'admin' AND org_id IS NULL")).scalar()
        bindings.add_binding(db, ctx, subject_type="principal", subject_id=pid, role_id=admin, scope_type="org")
        assert db.execute(text("SELECT first_factor_grant_until FROM identity_users WHERE principal_id = :p"),
                          {"p": pid}).scalar() is None


def test_f_under_enforce_the_legacy_key_acts_as_its_legacy_principal_not_ceo(engine, monkeypatch):
    """Round 2 (f): with the console's legacy key, the transport set current_api_principal='ceo'
    and every enforced handler recorded the person seat as the actor. Under enforce it is now the
    legacy principal identity mapped the key to."""
    from fastapi.testclient import TestClient

    from apps.api.app import main
    from boss_engine.config import get_settings
    from boss_engine.identity import legacy, migration

    key = "legacy-console-key-round2-0000000000000"
    for settings in (main.settings, get_settings()):
        monkeypatch.setattr(settings, "require_api_auth", True)
        monkeypatch.setattr(settings, "api_keys", key)
        monkeypatch.setattr(settings, "api_key_principals", f"{key}:ceo")
        monkeypatch.setattr(settings, "legacy_console_api_key", key)
        monkeypatch.setattr(settings, "authz_mode", "enforce")
    legacy.invalidate_cache()
    migration.forget_seeded()
    res = TestClient(main.app).get("/v1/auth/session", headers={"Authorization": f"Bearer {key}"})
    assert res.status_code == 200, res.text
    assert res.json()["principal_member_id"] == legacy.LEGACY_CONSOLE_PRINCIPAL


def test_the_audit_logs_actor_filter_is_not_the_actor_rule(engine):
    """Compose walk: the actor rule (F2/H7) rewrote EVERY `actor` query parameter to the caller's
    own actor — including the audit log's "who did it" FILTER, so an Owner's audit log showed only
    the Owner's own events (empty on a fresh install). A filter is marked and left alone."""
    from fastapi.testclient import TestClient

    from apps.api.app.main import app
    from apps.api.tests.identity_fixtures import IdentityWorld
    from boss_engine.identity import audit

    world = IdentityWorld.create(engine)
    owner, other = world.person("owner"), world.person("someone")
    world.bind(owner, "owner")
    with engine.begin() as db:
        audit.record(audit.SecurityEvent(org_id=world.org, event_type="user.invited",
                                         outcome=audit.Outcome.SUCCESS, actor_principal_id=other.id,
                                         actor_kind="user", target_type="user", target_id="usr_x"), db=db)
    client = TestClient(app)
    with world.acting_as(world.ctx(owner)):
        everyone = client.get("/v1/identity/audit").json()["events"]
        only_other = client.get(f"/v1/identity/audit?actor={other.id}").json()["events"]
    assert any(e["actor"]["id"] == other.id for e in everyone)
    assert only_other and all(e["actor"]["id"] == other.id for e in only_other)
