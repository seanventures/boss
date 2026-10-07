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
