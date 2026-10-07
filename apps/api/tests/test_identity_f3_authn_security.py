"""F3: regression tests for the authentication findings of the identity security test
(scratchpad identity/sec-authn-findings.md, id-sec-authn/repro_authn.py). Each test fails on the
integration branch before its fix (9070c34c) and names its finding.

Run through the real app (TestClient) with S1's test world (fakes of the other streams).
"""
from __future__ import annotations

from sqlalchemy import text

from apps.api.tests.identity_s1_world import (  # noqa: F401  (world is a fixture)
    HEADERS,
    ORIGIN,
    STRONG,
    csrf,
    fresh_client,
    make_user,
    mark_claimed,
    sign_in,
    totp_now,
    world,
)
from boss_engine.config import get_settings
from boss_engine.identity import authn


def _session_hash(client) -> str:
    return authn.hash_token(client.cookies.get(authn.SESSION_COOKIE) or "")


def _me_csrf(client) -> dict[str, str]:
    me = client.get("/v1/identity/me")
    assert me.status_code == 200, me.text
    return {**HEADERS, "X-Boss-CSRF": me.json()["csrf_token"]}


def _mark_signed_in_before(engine, pid: str) -> None:
    with engine.begin() as db:
        db.execute(text("UPDATE identity_users SET last_login_at = now() - interval '3 days' "
                        "WHERE principal_id = :p"), {"p": pid})


# ---- H1: an unenrolled MFA-required person gets an ENROLMENT-ONLY session ---------------------------

def test_h1_password_only_session_of_an_mfa_required_admin_is_enrolment_only(world, engine):
    mark_claimed(engine)
    make_user(engine, "admin2@example.test", role="admin")
    client = fresh_client()
    r = sign_in(client, "admin2@example.test")
    assert r.status_code == 200, r.text
    assert r.json()["next"] == "enrol" and r.json()["mfa_enrolment_required"] is True
    # who am I: allowed, with the banner
    me = client.get("/v1/identity/me")
    assert me.status_code == 200
    assert any(b["kind"] == "mfa_enrolment_required" for b in me.json()["banners"])
    # anything else: refused, reads and writes alike
    people = client.get("/v1/identity/users")
    assert people.status_code == 403 and people.json()["error"] == "mfa_enrolment_required", people.text
    keys = client.post("/v1/identity/me/api-keys", headers=_me_csrf(client),
                       json={"name": "k", "scopes": ["project:read"]})
    assert keys.status_code == 403 and keys.json()["error"] == "mfa_enrolment_required", keys.text


def test_h1_enrolment_only_is_refused_in_shadow_mode_too(world, engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "authz_mode", "shadow")
    mark_claimed(engine)
    make_user(engine, "admin3@example.test", role="admin")
    client = fresh_client()
    assert sign_in(client, "admin3@example.test").status_code == 200
    # a legacy (main) route is in shadow: the guard still refuses an enrolment-only session
    r = client.get("/v1/projects")
    assert r.status_code == 403 and r.json().get("error") == "mfa_enrolment_required", r.text


def test_h1_the_first_sign_in_may_enrol_the_first_factor_then_the_session_is_full(world, engine):
    mark_claimed(engine)
    make_user(engine, "first@example.test", role="admin")
    client = fresh_client()
    r = sign_in(client, "first@example.test")
    assert r.json().get("enrolment_grant") is True
    begin = client.post("/v1/identity/me/mfa/totp", headers=_me_csrf(client))
    assert begin.status_code == 200, begin.text
    confirm = client.post("/v1/identity/me/mfa/totp/confirm", headers=_me_csrf(client),
                          json={"code": totp_now(begin.json()["secret"])})
    assert confirm.status_code == 200, confirm.text
    assert client.get("/v1/identity/users").status_code == 200


def test_h1_a_later_password_only_sign_in_cannot_enrol_a_first_factor(world, engine):
    """The tester's repro: an attacker holding only the admin's password enrols THEIR factor."""
    mark_claimed(engine)
    pid = make_user(engine, "victim-admin@example.test", role="admin")
    _mark_signed_in_before(engine, pid)
    client = fresh_client()
    r = sign_in(client, "victim-admin@example.test")
    assert r.status_code == 200 and r.json()["next"] == "enrol"
    assert r.json().get("enrolment_grant") is False
    totp = client.post("/v1/identity/me/mfa/totp", headers=_me_csrf(client))
    assert totp.status_code == 403 and totp.json()["error"] == "enrolment_grant_required", totp.text
    pk = client.post("/v1/identity/me/passkeys/options", headers=_me_csrf(client))
    assert pk.status_code == 403 and pk.json()["error"] == "enrolment_grant_required", pk.text


def test_h1_the_first_factor_window_is_fifteen_minutes(world, engine):
    mark_claimed(engine)
    make_user(engine, "slow@example.test", role="admin")
    client = fresh_client()
    assert sign_in(client, "slow@example.test").status_code == 200
    with engine.begin() as db:
        db.execute(text("UPDATE identity_sessions SET enrol_grant_until = now() - interval '1 second' "
                        "WHERE token_hash = :h"), {"h": _session_hash(client)})
    totp = client.post("/v1/identity/me/mfa/totp", headers=_me_csrf(client))
    assert totp.status_code == 403 and totp.json()["error"] == "enrolment_grant_required"


def test_h1_an_admin_mfa_reset_grants_the_next_sign_in_once(world, engine):
    from apps.api.tests.identity_s1_world import promote_fresh_mfa

    mark_claimed(engine)
    pid = make_user(engine, "reset-me@example.test", role="admin")
    _mark_signed_in_before(engine, pid)
    owner = make_user(engine, "boss-owner@example.test", role="owner")
    oc = fresh_client()
    assert sign_in(oc, "boss-owner@example.test").status_code == 200
    promote_fresh_mfa(engine, oc.cookies.get(authn.SESSION_COOKIE))
    reset = oc.post(f"/v1/identity/users/{pid}/reset-mfa", headers=_me_csrf(oc))
    assert reset.status_code == 200, reset.text
    assert owner
    client = fresh_client()
    assert sign_in(client, "reset-me@example.test").json().get("enrolment_grant") is True
    assert client.post("/v1/identity/me/mfa/totp", headers=_me_csrf(client)).status_code == 200
    # consumed: a second password-only sign-in gets no grant
    again = fresh_client()
    assert sign_in(again, "reset-me@example.test").json().get("enrolment_grant") is False


# ---- H2: emailed links, passkeys and SSO only ever use the BOUND public origin -----------------------

EVIL = "evil.attacker.test"
EVIL_HEADERS = {"Host": EVIL, "Origin": f"http://{EVIL}", "Sec-Fetch-Site": "same-origin"}


def _unbound(monkeypatch) -> None:
    """The default install: BOSS_PUBLIC_URL unset and nothing bound yet."""
    monkeypatch.setattr(get_settings(), "public_url", "")


def test_h2_no_reset_link_is_built_from_the_host_header(world, engine, monkeypatch):
    """The tester's repro: the reset link carried the attacker's Host. Now: refused, no mail."""
    world.smtp = True
    _unbound(monkeypatch)
    make_user(engine, "victim@example.test", role="admin")
    r = fresh_client().post("/v1/auth/password/forgot", json={"email": "victim@example.test"},
                            headers=EVIL_HEADERS)
    assert r.status_code == 409 and r.json()["error"] == "public_origin_unset", r.text
    assert world.mail == []
    # the same answer for an unknown address: no oracle
    r2 = fresh_client().post("/v1/auth/password/forgot", json={"email": "nobody@example.test"},
                             headers=EVIL_HEADERS)
    assert (r2.status_code, r2.json()) == (r.status_code, r.json())


def test_h2_magic_link_and_signup_refuse_without_a_bound_origin(world, engine, monkeypatch):
    world.smtp = True
    _unbound(monkeypatch)
    make_user(engine, "ml@example.test")
    from apps.api.tests.identity_s1_world import set_policy

    set_policy(engine, email_magic_link=True, signup_enabled=True)
    r = fresh_client().post("/v1/auth/magic-link", json={"email": "ml@example.test"}, headers=EVIL_HEADERS)
    assert r.status_code == 409 and r.json()["error"] == "public_origin_unset"
    s = fresh_client().post("/v1/auth/signup", json={"name": "x", "email": "new@example.test"},
                            headers=EVIL_HEADERS)
    assert s.status_code == 409
    assert world.mail == []


LOCAL = "http://localhost:3000"   # a local install: plain http on loopback is a secure origin


def _local_client():
    from fastapi.testclient import TestClient
    from apps.api.app.main import app

    return TestClient(app, base_url=LOCAL)


def _claim_with_setup_token(capsys, origin: str):
    import re

    client = _local_client()
    client.get("/v1/auth/setup/status", headers={"Origin": origin})
    token = re.search(r"/claim#t=(bst_[A-Za-z0-9_\-]+)", capsys.readouterr().out).group(1)
    return client, client.post("/v1/auth/setup/claim", headers={"Origin": origin, "Sec-Fetch-Site": "same-origin"},
                               json={"name": "First Owner", "email": "first@example.test", "password": STRONG,
                                     "setup_token": token})


def test_h2_the_setup_token_claim_binds_the_browsers_origin_and_links_use_it(world, engine, monkeypatch,
                                                                             capsys):
    world.smtp = True
    _unbound(monkeypatch)
    _client, res = _claim_with_setup_token(capsys, LOCAL)
    assert res.status_code == 200, res.text
    with engine.begin() as db:
        bound = db.execute(text("SELECT policy->>'public_origin' FROM identity_settings")).scalar()
    assert bound == LOCAL
    # a reset requested through an attacker's Host still points at the bound origin
    make_user(engine, "victim@example.test", role="operator")
    r = _local_client().post("/v1/auth/password/forgot", json={"email": "victim@example.test"},
                             headers={"Host": EVIL, "Origin": LOCAL, "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 202, r.text
    _subject, body = world.last_mail_to("victim@example.test")
    assert f"{LOCAL}/reset#t=" in body and EVIL not in body


def test_h2_the_legacy_password_claim_binds_the_origin_too(world, engine, monkeypatch):
    _unbound(monkeypatch)
    monkeypatch.setattr(get_settings(), "legacy_console_password", "the-old-shared-password")
    r = _local_client().post("/v1/auth/setup/claim", headers={"Origin": LOCAL, "Sec-Fetch-Site": "same-origin"},
                             json={"name": "O", "email": "o@example.test", "password": STRONG,
                                   "legacy_password": "the-old-shared-password"})
    assert r.status_code == 200, r.text
    with engine.begin() as db:
        assert db.execute(text("SELECT policy->>'public_origin' FROM identity_settings")).scalar() == LOCAL


def test_h2_passkeys_never_take_their_rp_id_from_host(world, engine, monkeypatch):
    _unbound(monkeypatch)
    r = fresh_client().post("/v1/auth/webauthn/options", json={"purpose": "login"}, headers=EVIL_HEADERS)
    assert r.status_code == 409 and r.json()["error"] == "passkey_unavailable", r.text


def test_h2_posture_and_sign_in_options_say_when_no_origin_is_bound(world, engine, monkeypatch):
    from boss_engine.identity import posture

    _unbound(monkeypatch)
    opts = fresh_client().get("/v1/auth/options", headers=EVIL_HEADERS).json()
    assert opts["public_origin"] is None and opts["public_origin_source"] is None
    with engine.begin() as db:
        from boss_engine.identity.authn_support import default_org_id

        check = posture.public_origin_check(default_org_id(db), db=db)
    assert check["status"] == "critical" and "BOSS_PUBLIC_URL" in check["fix"]
    monkeypatch.setattr(get_settings(), "public_url", ORIGIN)
    opts = fresh_client().get("/v1/auth/options").json()
    assert opts["public_origin"] == ORIGIN and opts["public_origin_source"] == "env"


# ---- L1: the network claim believes X-Forwarded-Host only from a trusted proxy ---------------------

def test_l1_network_claim_ignores_x_forwarded_host_from_an_untrusted_peer(world, engine, monkeypatch):
    from apps.api.app import identity_guard
    from apps.api.tests.identity_s1_world import make_network_operator

    world.network_bridge = True
    make_network_operator(engine)
    monkeypatch.setattr(identity_guard, "_legacy_method", lambda: "network")
    _unbound(monkeypatch)
    monkeypatch.setattr(get_settings(), "trusted_proxies", "10.9.9.9/32")
    evil = "https://evil.example"
    # The TestClient peer ("testclient") is not a trusted proxy: its X-Forwarded-Host is ignored,
    # so an attacker-chosen origin no longer matches the "addressed" host.
    r = fresh_client().post("/v1/auth/setup/claim",
                            headers={"Origin": evil, "X-Forwarded-Host": "evil.example"},
                            json={"name": "Op", "email": "op@example.test", "confirm_origin": evil})
    assert r.status_code == 400 and r.json()["error"] == "origin_mismatch", r.text


# ---- L4: one claim per org, enforced by the database ----------------------------------------------

def test_l4_a_second_bootstrap_claimed_row_is_refused_by_the_database(world, engine):
    import pytest
    from sqlalchemy.exc import IntegrityError

    mark_claimed(engine)
    with pytest.raises(IntegrityError) as exc:
        with engine.begin() as db:
            org = db.execute(text("SELECT org_id FROM identity_security_events LIMIT 1")).scalar()
            db.execute(text("INSERT INTO identity_security_events (org_id, seq, stream, event_type, "
                            "outcome, prev_hash, hash) VALUES (:o, 999, 'security', 'bootstrap.claimed', "
                            "'success', :p, :h)"), {"o": org, "p": b"\0" * 32, "h": b"\2" * 32})
    assert "identity_one_claim_per_org" in str(exc.value)
