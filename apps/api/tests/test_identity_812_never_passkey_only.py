"""A passkey is never the only way in (BOSSAPP-812, operator on boss-1, 2026-11-04).

The first Owner claimed with a passkey only. In any other browser profile there was nothing to
sign in with. Now:

  * a claim always sets a password (test_identity_authn_claim pins the refusal);
  * an account that holds a passkey but no password (claimed before this change) is told, once,
    to add one (`no_password` banner, dismissible for good), and is never locked out: setting
    the first password needs a fresh second factor, as before, and then the banner is gone.
"""
from __future__ import annotations

from sqlalchemy import text

from apps.api.tests.identity_s1_world import (  # noqa: F401  (world is a fixture)
    STRONG,
    csrf,
    fresh_client,
    make_user,
    promote_fresh_mfa,
    sign_in,
    world,
)
from boss_engine.identity import authn


def _kinds(client) -> set[str]:
    me = client.get("/v1/identity/me")
    assert me.status_code == 200, me.text
    return {b["kind"] for b in me.json()["banners"]}


def _passkey_only(engine, pid: str) -> None:
    with engine.begin() as db:
        db.execute(text("INSERT INTO identity_webauthn_credentials (id, principal_id, credential_id, "
                        "public_key, name) VALUES ('pky_812', :p, '\\x01'::bytea, '\\x02'::bytea, "
                        "'Mac · Chrome')"), {"p": pid})
        db.execute(text("UPDATE identity_users SET password_hash = NULL WHERE principal_id = :p"),
                   {"p": pid})


def test_a_passkey_only_account_is_asked_once_for_a_password_and_can_set_one(world, engine):
    pid = make_user(engine, "pk812@example.test")
    client = fresh_client()
    assert sign_in(client, "pk812@example.test").status_code == 200
    assert "no_password" not in _kinds(client)          # a password: nothing to say
    _passkey_only(engine, pid)
    me = client.get("/v1/identity/me").json()
    banner = [b for b in me["banners"] if b["kind"] == "no_password"]
    assert banner and banner[0]["policy"] == "dismissible"
    assert banner[0]["message"] == "Add a password so you can sign in from any browser."
    assert me["sign_in"]["password"] is False and me["sign_in"]["passkeys"] == 1

    # setting the first password needs a fresh second factor (unchanged), then it is gone
    stale = client.post("/v1/identity/me/password", json={"new": STRONG + "-x"}, headers=csrf(client))
    assert stale.status_code == 401 and stale.json()["error"] == "step_up_required"
    promote_fresh_mfa(engine, client.cookies.get(authn.SESSION_COOKIE))
    ok = client.post("/v1/identity/me/password", json={"new": STRONG + "-x"}, headers=csrf(client))
    assert ok.status_code == 200, ok.text
    assert "no_password" not in _kinds(client)


def test_closing_the_banner_hides_it_for_good(world, engine):
    pid = make_user(engine, "pk812b@example.test", role="owner")
    client = fresh_client()
    assert sign_in(client, "pk812b@example.test").status_code == 200
    _passkey_only(engine, pid)
    # an Owner signed in with the passkey: a session with a fresh second factor
    promote_fresh_mfa(engine, client.cookies.get(authn.SESSION_COOKIE))
    assert "no_password" in _kinds(client)
    closed = client.post("/v1/identity/me/banners/no_password/dismiss", headers=csrf(client))
    assert closed.status_code in (200, 204), closed.text
    assert "no_password" not in _kinds(client)
    assert "no_password" not in _kinds(client)


def test_an_account_with_no_factor_and_no_password_gets_no_such_banner(world, engine):
    """Only a passkey-holder is pointed at a password: a TOTP or email-code person can already
    sign in from any browser."""
    pid = make_user(engine, "plain812@example.test")
    client = fresh_client()
    assert sign_in(client, "plain812@example.test").status_code == 200
    with engine.begin() as db:
        db.execute(text("UPDATE identity_users SET password_hash = NULL WHERE principal_id = :p"), {"p": pid})
    assert "no_password" not in _kinds(client)
