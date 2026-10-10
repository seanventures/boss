"""The org's only Owner can always enrol their first second factor on their own session
(tester round 2, N12).

Seen: the claiming Owner (the only ceo-seat holder) signed in after the claim and was told "You
can't add a second factor from this session. Ask an admin to reset your second factor". A later
password-only sign-in gets no first-factor grant (H1, so a stolen password cannot bind the
thief's factor), and resetting an Owner's MFA needs ANOTHER Owner. With one Owner there was
nobody to ask: the Owner was locked out, and nobody could approve a tier-2 deploy.

Product, not harness: any Owner who did not finish enrolling during the claim hit it.
"""
from __future__ import annotations

from sqlalchemy import text

from apps.api.tests.identity_s1_world import (  # noqa: F401  (world is a fixture)
    HEADERS,
    fresh_client,
    make_user,
    mark_claimed,
    sign_in,
    totp_now,
    world,
)


def _me_csrf(client) -> dict[str, str]:
    me = client.get("/v1/identity/me")
    assert me.status_code == 200, me.text
    return {**HEADERS, "X-Boss-CSRF": me.json()["csrf_token"]}


def _signed_in_before(engine, pid: str) -> None:
    with engine.begin() as db:
        db.execute(text("UPDATE identity_users SET last_login_at = now() - interval '3 days' "
                        "WHERE principal_id = :p"), {"p": pid})


def test_the_only_owner_enrols_their_first_factor_on_a_later_sign_in(world, engine):
    pid = make_user(engine, "only-owner@example.test", role="owner")
    mark_claimed(engine)
    _signed_in_before(engine, pid)            # the claim was a sign-in; this is a later one
    client = fresh_client()
    r = sign_in(client, "only-owner@example.test")
    assert r.status_code == 200 and r.json()["next"] == "enrol"
    assert r.json().get("enrolment_grant") is True, r.json()
    begin = client.post("/v1/identity/me/mfa/totp", headers=_me_csrf(client))
    assert begin.status_code == 200, begin.text
    confirm = client.post("/v1/identity/me/mfa/totp/confirm", headers=_me_csrf(client),
                          json={"code": totp_now(begin.json()["secret"])})
    assert confirm.status_code == 200, confirm.text


def test_with_another_owner_to_reset_them_a_password_alone_still_cannot_enrol(world, engine):
    """H1 still holds wherever someone CAN reset: a later password-only sign-in binds nothing."""
    pid = make_user(engine, "owner-a@example.test", role="owner")
    make_user(engine, "owner-b@example.test", role="owner")
    mark_claimed(engine)
    _signed_in_before(engine, pid)
    client = fresh_client()
    r = sign_in(client, "owner-a@example.test")
    assert r.json().get("enrolment_grant") is False
    totp = client.post("/v1/identity/me/mfa/totp", headers=_me_csrf(client))
    assert totp.status_code == 403 and totp.json()["error"] == "enrolment_grant_required", totp.text


def test_an_admin_is_not_an_owner_and_keeps_the_reset_path(world, engine):
    make_user(engine, "the-owner@example.test", role="owner")
    pid = make_user(engine, "an-admin@example.test", role="admin")
    mark_claimed(engine)
    _signed_in_before(engine, pid)
    r = sign_in(fresh_client(), "an-admin@example.test")
    assert r.json().get("enrolment_grant") is False
