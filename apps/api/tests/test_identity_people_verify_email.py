"""An admin marks a person's email verified (identity-provider parity, FX-C): POST
/v1/identity/users/{id}/verify-email sets email_verified_at once, is audited, shows in the person
view as `email_verified`, needs user:update (with step-up), and keeps the Owner guard."""
from __future__ import annotations

from sqlalchemy import text

from apps.api.tests.identity_s1_world import (  # noqa: F401  (world is a fixture)
    HEADERS,
    fresh_client,
    make_user,
    mark_claimed,
    promote_fresh_mfa,
    sign_in,
    world,
)
from boss_engine.identity import authn


def _csrf(client) -> dict[str, str]:
    me = client.get("/v1/identity/me")
    assert me.status_code == 200, me.text
    return {**HEADERS, "X-Boss-CSRF": me.json()["csrf_token"]}


def _unverify(engine, pid: str) -> None:
    with engine.begin() as db:
        db.execute(text("UPDATE identity_users SET email_verified_at = NULL WHERE principal_id = :p"), {"p": pid})


def _admin(engine, email: str, role: str = "admin"):
    make_user(engine, email, role=role)
    c = fresh_client()
    assert sign_in(c, email).status_code == 200
    promote_fresh_mfa(engine, c.cookies.get(authn.SESSION_COOKIE))
    return c


def test_an_admin_marks_an_email_verified_once_and_it_is_audited(world, engine):
    mark_claimed(engine)
    pid = make_user(engine, "unverified@example.test", role="viewer")
    _unverify(engine, pid)
    admin = _admin(engine, "verifier@example.test")
    before = admin.get(f"/v1/identity/users/{pid}")
    assert before.status_code == 200 and before.json()["email_verified"] is False, before.text
    r = admin.post(f"/v1/identity/users/{pid}/verify-email", headers=_csrf(admin))
    assert r.status_code == 200, r.text
    assert r.json()["email_verified"] is True
    again = admin.post(f"/v1/identity/users/{pid}/verify-email", headers=_csrf(admin))
    assert again.status_code == 200 and again.json()["email_verified"] is True
    with engine.begin() as db:
        n = db.execute(text("SELECT count(*) FROM identity_security_events WHERE event_type = 'user.email_verified' "
                            "AND target_id = :p"), {"p": pid}).scalar()
    assert n == 1


def test_a_viewer_cannot_mark_an_email_verified(world, engine):
    mark_claimed(engine)
    pid = make_user(engine, "someone@example.test", role="viewer")
    _unverify(engine, pid)
    viewer = _admin(engine, "just-a-viewer@example.test", role="viewer")
    r = viewer.post(f"/v1/identity/users/{pid}/verify-email", headers=_csrf(viewer))
    assert r.status_code == 403, r.text
    with engine.begin() as db:
        assert db.execute(text("SELECT email_verified_at FROM identity_users WHERE principal_id = :p"),
                          {"p": pid}).scalar() is None


def test_an_admin_cannot_vouch_for_an_owner(world, engine):
    mark_claimed(engine)
    owner = make_user(engine, "the-owner@example.test", role="owner")
    _unverify(engine, owner)
    admin = _admin(engine, "mere-admin@example.test")
    r = admin.post(f"/v1/identity/users/{owner}/verify-email", headers=_csrf(admin))
    assert r.status_code == 403, r.text


def test_an_unknown_person_is_not_found(world, engine):
    mark_claimed(engine)
    admin = _admin(engine, "finder@example.test")
    assert admin.post("/v1/identity/users/usr_nobody/verify-email", headers=_csrf(admin)).status_code == 404
