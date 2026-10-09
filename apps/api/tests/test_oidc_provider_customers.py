"""BOSS as an OIDC provider: customer pools (F6 §6.3, S6.1, S6.2; stream S6 phase 2), through the
real app: hosted sign-up with email verification, TOTP enrolment and sign-in, passkeys, password
reset, the account page, the pool-user admin API — and the adversarial list for the realm:

C1 a pool user is never a BOSS principal and its session never a BOSS session · C2 no cross-pool or
cross-org leakage (sessions, challenges, users, subjects, admin) · C3 anti-enumeration and lockout ·
C4 TOTP replay and brute force · C5 passkeys bound to their user and pool · C6 CSRF on the hosted
steps · C7 disabling, resetting and revoking end everything · C8 session fixation.
"""
from __future__ import annotations

import re

import jwt
import pytest
from sqlalchemy import text

from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.oidc_world import (
    ORIGIN, STRONG_PW, SoftAuthenticator, customer_world, field_of, hosted, hosted_json, last_code,
    pkce, query_of, totp_code, totp_from_page)
from boss_engine.db import begin
from boss_engine.oidc_provider import pools as oidc_pools


@pytest.fixture
def cw(engine, monkeypatch):
    w, mail = customer_world(engine, monkeypatch)
    client, secret = w.client(pool_id=w.cpool["id"], name="Bank")
    w.cclient, w.csecret, w.mail = client, secret, mail
    return w


def _authorize(w, client=None, **extra):
    verifier, challenge = pkce()
    r, params = w.authorize((client or w.cclient)["id"], challenge=challenge,
                            extra={"scope": "openid email profile offline_access", **extra})
    return r, params, verifier


def _consent(w, page):
    form = {"interaction": field_of(page.text, "interaction"), "csrf": field_of(page.text, "csrf"), "decision": "allow"}
    return hosted_like(w, f"/oidc/{w.org}/consent", form)


def hosted_like(w, path, form):
    from urllib.parse import urlencode

    from apps.api.tests.oidc_world import HOSTED

    return w.http.post(path, content=urlencode(form), headers=HOSTED, follow_redirects=False)


def _signup(w, email="cara@example.test"):
    """authorize -> sign-up -> email code -> TOTP enrolment -> (consent page). Returns (page, secret, params, verifier)."""
    r, params, verifier = _authorize(w)
    assert r.status_code == 200 and f"Sign in to {w.cclient['name']}" in r.text, r.text[:300]
    interaction = field_of(r.text, "interaction")
    page = w.http.get(f"/oidc/{w.org}/account", params={"view": "signup", "interaction": interaction})
    assert page.status_code == 200 and f"Create your {w.cclient['name']} account" in page.text
    r = hosted(w, "signup", {"interaction": interaction, "name": "Cara", "email": email, "password": STRONG_PW})
    assert r.status_code == 200 and "Check your email" in r.text, r.text[:400]
    r = hosted(w, "verify-email", {"interaction": interaction, "code": last_code(w.mail)})
    assert r.status_code == 200 and "authenticator app" in r.text, r.text[:400]
    secret = totp_from_page(r.text)
    r = hosted(w, "enrol-totp", {"interaction": interaction, "code": totp_code(secret)})
    return r, secret, params, verifier


def _finish(w, page, params, verifier):
    if page.status_code == 200:
        page = _consent(w, page)
    assert page.status_code == 303, (page.status_code, page.text[:300])
    q = query_of(page.headers["location"])
    assert q["iss"] == w.issuer and q["state"] == params["state"]
    tok = w.exchange(w.cclient, w.csecret, q["code"], verifier)
    assert tok.status_code == 200, tok.text
    return tok.json()


def _sign_in(w, email, secret, offset=0, **extra):
    r, params, verifier = _authorize(w, **extra)
    interaction = field_of(r.text, "interaction")
    r = hosted(w, "signin", {"interaction": interaction, "email": email, "password": STRONG_PW})
    assert r.status_code == 200 and "Second step" in r.text, r.text[:300]
    return hosted(w, "mfa", {"interaction": interaction, "code": totp_code(secret, offset)}), params, verifier


def _user(w, email):
    with begin() as db:
        return db.execute(text("SELECT * FROM oidc_pool_users WHERE lower(email) = lower(:e) AND pool_id = :p"),
                          {"e": email, "p": w.cpool["id"]}).mappings().first()


# ---- the flow ----------------------------------------------------------------------------------------

def test_sign_up_verify_enrol_consent_and_sign_in_again(cw):
    page, secret, params, verifier = _signup(cw)
    assert "Bank wants to sign you in" in page.text
    assert cw.http.cookies.get("__Host-boss_app_session", domain="boss.oidc.test")
    tok = _finish(cw, page, params, verifier)
    idt = cw.verify(tok["id_token"], audience=cw.cclient["id"])
    assert idt["acr"] == "urn:boss:acr:mfa" and "totp" in idt["amr"]
    info = cw.userinfo(tok["access_token"]).json()
    assert info["email"] == "cara@example.test" and info["email_verified"] is True and info["name"] == "Cara"
    assert not info["sub"].startswith("opu_")
    # signed in already: the next authorization is silent
    r, params2, v2 = _authorize(cw, prompt="none")
    assert r.status_code == 302 and "code=" in r.headers["location"]
    # prompt=login asks again: password, then the (next) TOTP code
    r, params3, v3 = _sign_in(cw, "cara@example.test", secret, offset=1, prompt="login")
    assert r.status_code == 303 and "code=" in r.headers["location"], r.text[:300]


def test_a_pool_without_mfa_signs_in_with_a_password(engine, monkeypatch):
    w, mail = customer_world(engine, monkeypatch, mfa="off")
    client, secret = w.client(pool_id=w.cpool["id"], name="Shop", first_party=True)
    w.cclient, w.csecret, w.mail = client, secret, mail
    r, params, verifier = _authorize(w, scope="openid email")
    interaction = field_of(r.text, "interaction")
    hosted(w, "signup", {"interaction": interaction, "name": "Dee", "email": "dee@example.test", "password": STRONG_PW})
    r = hosted(w, "verify-email", {"interaction": interaction, "code": last_code(mail)})
    tok = _finish(w, r, params, verifier)
    assert w.verify(tok["id_token"], audience=client["id"])["acr"] == "urn:boss:acr:pwd"
    assert "refresh_token" not in tok


def test_require_mfa_on_the_client_forces_enrolment_in_an_optional_pool(engine, monkeypatch):
    w, mail = customer_world(engine, monkeypatch, mfa="optional")
    client, secret = w.client(pool_id=w.cpool["id"], name="Vault", require_mfa=True)
    w.cclient, w.csecret, w.mail = client, secret, mail
    r, secret_totp, params, verifier = _signup(w, "eve@example.test")
    _finish(w, r, params, verifier)


# ---- C1 a pool user is never a BOSS principal -----------------------------------------------------

def test_c1_a_pool_user_has_no_principal_and_its_session_and_tokens_open_nothing_in_boss(cw, monkeypatch):
    from boss_engine.config import get_settings

    monkeypatch.setattr(get_settings(), "authz_mode", "enforce")      # the product default (H2)
    monkeypatch.setattr(get_settings(), "authz_router_modes", "")
    page, _secret, params, verifier = _signup(cw)
    tok = _finish(cw, page, params, verifier)
    user = _user(cw, "cara@example.test")
    with begin() as db:
        assert db.execute(text("SELECT count(*) FROM identity_principals WHERE id = :u"), {"u": user["id"]}).scalar() == 0
        assert db.execute(text("SELECT count(*) FROM identity_sessions s JOIN identity_principals p ON p.id = s.principal_id "
                               "WHERE p.display_name = 'Cara'")).scalar() == 0
    app_cookie = cw.http.cookies.get("__Host-boss_app_session", domain="boss.oidc.test")
    from fastapi.testclient import TestClient

    from apps.api.app import main

    clean = TestClient(main.app, base_url=ORIGIN)
    for headers in ({"Cookie": f"__Host-boss_app_session={app_cookie}"}, {"Cookie": f"__Host-boss_session={app_cookie}"},
                    {"Authorization": f"Bearer {app_cookie}"}, {"Authorization": f"Bearer {tok['access_token']}"},
                    {"Authorization": f"Bearer {tok['id_token']}"}):
        assert clean.get("/v1/identity/me", headers=headers).status_code == 401, headers
        assert clean.get("/v1/projects", headers=headers).status_code in (401, 403), headers


def test_c1_a_workforce_client_never_accepts_a_customer_session(cw):
    _signup(cw)
    staff_client, _ = cw.client(name="Staff app")
    r, _ = cw.authorize(staff_client["id"])          # the jar holds only the app session
    assert r.status_code == 302 and "/login?next=" in r.headers["location"]


def test_c1_a_pool_user_cannot_be_given_a_boss_role_or_an_app_role_elsewhere(cw):
    _signup(cw)
    user = _user(cw, "cara@example.test")
    staff_client, _ = cw.client(name="Staff app")
    with begin() as db:
        oidc_pools.create_role(db, actor="usr_test", client=staff_client, role_key="admin", name="Admin")
        with pytest.raises(LookupError):
            oidc_pools.assign_role(db, actor="usr_test", client=staff_client, subject_ref=user["id"], role_key="admin")
        from boss_engine.oidc_provider import pools

        assert pools.workforce_principal(db, user["id"]) is None


# ---- C2 no cross-pool or cross-org leakage ---------------------------------------------------------

def test_c2_a_session_of_one_pool_is_nothing_in_another(cw):
    _signup(cw)
    with begin() as db:
        other_pool = oidc_pools.create(db, org_id=cw.org, actor="usr_test",
                                       body={"name": "Other", "kind": "customer", "signup": "open", "mfa": "off"})
    other, _ = cw.client(pool_id=other_pool["id"], name="Other app")
    r, _ = cw.authorize(other["id"], extra={"prompt": "none", "scope": "openid"})
    assert query_of(r.headers["location"])["error"] == "login_required"
    r, _ = cw.authorize(other["id"], extra={"scope": "openid"})
    assert r.status_code == 200 and "Sign in to Other app" in r.text
    # the same email in the other pool is another user: the password of pool A opens nothing there
    interaction = field_of(r.text, "interaction")
    r = hosted(cw, "signin", {"interaction": interaction, "email": "cara@example.test", "password": STRONG_PW})
    assert r.status_code == 401


def test_c2_a_challenge_of_one_pool_is_not_spent_in_another(cw):
    r, params, verifier = _authorize(cw)
    interaction = field_of(r.text, "interaction")
    hosted(cw, "signup", {"interaction": interaction, "name": "Cara", "email": "cara@example.test", "password": STRONG_PW})
    code = last_code(cw.mail)
    with begin() as db:
        other_pool = oidc_pools.create(db, org_id=cw.org, actor="usr_test",
                                       body={"name": "Other", "kind": "customer", "signup": "open", "mfa": "off"})
    # the form token is bound to the interaction: posting it for another pool is refused outright
    assert hosted(cw, "verify-email", {"pool": other_pool["id"], "code": code}).status_code == 403
    # with the other pool's own page token, the challenge still belongs to the first pool
    cw.http.get(f"/oidc/{cw.org}/account", params={"view": "signin", "pool": other_pool["id"]})
    r = hosted(cw, "verify-email", {"pool": other_pool["id"], "code": code})
    assert r.status_code == 401
    assert _user(cw, "cara@example.test")["status"] == "pending"


def test_c2_the_pool_user_admin_api_is_confined_to_its_org_and_customer_pools(cw, engine):
    from fastapi.testclient import TestClient

    from apps.api.app import main

    api = TestClient(main.app, raise_server_exceptions=False)
    _signup(cw)
    other = IdentityWorld.create(engine)
    owner = other.person("other-owner")
    other.bind(owner, "owner")
    with other.acting_as(other.ctx(owner)):
        assert api.get(f"/v1/oidc/pools/{cw.cpool['id']}/users").status_code == 404
        user = _user(cw, "cara@example.test")
        assert api.patch(f"/v1/oidc/pools/{cw.cpool['id']}/users/{user['id']}", json={"status": "disabled"}).status_code == 404
    mine = cw.ids.person("mine")
    cw.ids.bind(mine, "owner")
    with cw.ids.acting_as(cw.ids.ctx(mine)):
        assert api.get(f"/v1/oidc/pools/{cw.pool['id']}/users").status_code == 404     # a workforce pool has none
        listed = api.get(f"/v1/oidc/pools/{cw.cpool['id']}/users").json()["users"]
        assert [u["email"] for u in listed] == ["cara@example.test"] and "password_hash" not in listed[0]


def test_c2_subjects_differ_per_client_in_one_pool(cw):
    page, secret, params, verifier = _signup(cw)
    sub_a = cw.verify(_finish(cw, page, params, verifier)["id_token"], audience=cw.cclient["id"])["sub"]
    b, b_secret = cw.client(pool_id=cw.cpool["id"], name="Loans")
    verifier, challenge = pkce()
    r, p = cw.authorize(b["id"], challenge=challenge, extra={"scope": "openid"})
    r = _consent(cw, r)
    tok = cw.exchange(b, b_secret, query_of(r.headers["location"])["code"], verifier).json()
    assert jwt.decode(tok["id_token"], options={"verify_signature": False})["sub"] != sub_a


# ---- C3 anti-enumeration and lockout ---------------------------------------------------------------

def test_c3_unknown_email_and_wrong_password_look_the_same(cw):
    _signup(cw)
    cw.http.cookies.clear()
    r, _, _ = _authorize(cw)
    interaction = field_of(r.text, "interaction")
    wrong = hosted(cw, "signin", {"interaction": interaction, "email": "cara@example.test", "password": "x" * 14})
    unknown = hosted(cw, "signin", {"interaction": interaction, "email": "nobody@example.test", "password": "x" * 14})
    assert wrong.status_code == unknown.status_code == 401
    strip = lambda t: re.sub(r'value="[^"]*"|nonce-[\w-]+|nonce="[^"]+"', "", t)  # noqa: E731
    assert strip(wrong.text).replace("cara@example.test", "") == strip(unknown.text).replace("nobody@example.test", "")


def test_c3_signing_up_with_a_taken_email_reveals_nothing_and_verifies_nothing(cw):
    _signup(cw)
    cw.http.cookies.clear()
    before = len(cw.mail)
    r, _, _ = _authorize(cw)
    interaction = field_of(r.text, "interaction")
    again = hosted(cw, "signup", {"interaction": interaction, "name": "Mallory", "email": "CARA@example.test",
                                  "password": STRONG_PW})
    assert again.status_code == 200 and "Check your email" in again.text
    assert len(cw.mail) == before
    for code in ("000000", "123456"):
        assert hosted(cw, "verify-email", {"interaction": interaction, "code": code}).status_code == 401


def test_c3_ten_failures_lock_the_account_even_for_the_right_password(cw):
    _signup(cw)
    cw.http.cookies.clear()
    r, _, _ = _authorize(cw)
    interaction = field_of(r.text, "interaction")
    for _ in range(10):
        hosted(cw, "signin", {"interaction": interaction, "email": "cara@example.test", "password": "wrong-password-1"})
    r = hosted(cw, "signin", {"interaction": interaction, "email": "cara@example.test", "password": STRONG_PW})
    assert r.status_code == 429 and "Too many attempts" in r.text


# ---- C4 TOTP ------------------------------------------------------------------------------------------

def test_c4_a_totp_code_is_single_use_and_guessing_ends_the_attempt(cw):
    page, secret, params, verifier = _signup(cw)       # enrolment used the current step
    _finish(cw, page, params, verifier)
    r, _, _ = _authorize(cw, prompt="login")
    interaction = field_of(r.text, "interaction")
    hosted(cw, "signin", {"interaction": interaction, "email": "cara@example.test", "password": STRONG_PW})
    replay = hosted(cw, "mfa", {"interaction": interaction, "code": totp_code(secret)})
    assert replay.status_code == 401                    # the enrolment's step cannot sign in again
    for _ in range(3):
        assert hosted(cw, "mfa", {"interaction": interaction, "code": "000000"}).status_code == 401
    last = hosted(cw, "mfa", {"interaction": interaction, "code": "111111"})
    assert last.status_code == 429                      # five strikes: the half-done sign-in is gone
    assert hosted(cw, "mfa", {"interaction": interaction, "code": totp_code(secret, 1)}).status_code == 401


def test_c4_a_password_alone_never_finishes_a_pool_that_requires_mfa(cw):
    _signup(cw)
    cw.http.cookies.clear()
    r, _, _ = _authorize(cw)
    interaction = field_of(r.text, "interaction")
    r = hosted(cw, "signin", {"interaction": interaction, "email": "cara@example.test", "password": STRONG_PW})
    assert r.status_code == 200 and "Second step" in r.text
    assert not cw.http.cookies.get("__Host-boss_app_session", domain="boss.oidc.test")


# ---- C5 passkeys -------------------------------------------------------------------------------------

def _account_csrf(w, pool):
    page = w.http.get(f"/oidc/{w.org}/account", params={"pool": pool["id"]})
    assert "Your Bank customers account" in page.text, page.text[:300]
    return page


def test_c5_add_a_passkey_and_sign_in_with_it_alone(cw):
    page, secret, params, verifier = _signup(cw)
    _finish(cw, page, params, verifier)
    _account_csrf(cw, cw.cpool)
    opts = hosted_json(cw, "passkey-options", {"purpose": "register", "pool": cw.cpool["id"]})
    assert opts.status_code == 200, opts.text
    auth = SoftAuthenticator()
    done = hosted_json(cw, "passkey-verify", {"purpose": "register", "pool": cw.cpool["id"],
                                              "credential": auth.register(opts.json()["publicKey"])})
    assert done.status_code == 200, done.text
    cw.http.cookies.clear()
    r, params, verifier = _authorize(cw)
    interaction = field_of(r.text, "interaction")
    opts = hosted_json(cw, "passkey-options", {"purpose": "login", "interaction": interaction})
    v = hosted_json(cw, "passkey-verify", {"purpose": "login", "interaction": interaction,
                                           "credential": auth.assert_(opts.json()["publicKey"])})
    assert v.status_code == 200 and v.json()["next"].startswith(f"/oidc/{cw.org}/resume?interaction=")
    r = cw.http.get(v.json()["next"], follow_redirects=False)
    assert r.status_code == 302 and "code=" in r.headers["location"]       # consent was given at sign-up
    tok = cw.exchange(cw.cclient, cw.csecret, query_of(r.headers["location"])["code"], verifier).json()
    claims = cw.verify(tok["id_token"], audience=cw.cclient["id"])
    assert claims["acr"] == "urn:boss:acr:mfa" and "passkey" in claims["amr"]


def test_c5_a_passkey_cannot_sign_in_as_someone_else_or_in_another_pool(cw):
    page, secret, params, verifier = _signup(cw)
    _finish(cw, page, params, verifier)
    _account_csrf(cw, cw.cpool)
    opts = hosted_json(cw, "passkey-options", {"purpose": "register", "pool": cw.cpool["id"]})
    auth = SoftAuthenticator()
    hosted_json(cw, "passkey-verify", {"purpose": "register", "pool": cw.cpool["id"],
                                       "credential": auth.register(opts.json()["publicKey"])})
    cw.http.cookies.clear()
    r, _, _ = _authorize(cw)
    interaction = field_of(r.text, "interaction")
    opts = hosted_json(cw, "passkey-options", {"purpose": "login", "interaction": interaction})
    forged = auth.assert_(opts.json()["publicKey"], user_handle=b"opu_someone_else_entirely")
    assert hosted_json(cw, "passkey-verify", {"purpose": "login", "interaction": interaction,
                                              "credential": forged}).status_code == 401
    with begin() as db:
        other_pool = oidc_pools.create(db, org_id=cw.org, actor="usr_test",
                                       body={"name": "Other", "kind": "customer", "signup": "open", "mfa": "off",
                                             "passkeys": True})
    cw.http.get(f"/oidc/{cw.org}/account", params={"view": "signin", "pool": other_pool["id"]})
    opts = hosted_json(cw, "passkey-options", {"purpose": "login", "pool": other_pool["id"]})
    assert hosted_json(cw, "passkey-verify", {"purpose": "login", "pool": other_pool["id"],
                                              "credential": auth.assert_(opts.json()["publicKey"])}).status_code == 401
    # a ceremony for another origin is refused
    cw.http.get(f"/oidc/{cw.org}/account", params={"view": "signin", "interaction": interaction})
    opts = hosted_json(cw, "passkey-options", {"purpose": "login", "interaction": interaction})
    assert hosted_json(cw, "passkey-verify", {"purpose": "login", "interaction": interaction, "credential":
                       auth.assert_(opts.json()["publicKey"], origin="https://evil.test")}).status_code == 401


def test_c5_registering_a_passkey_needs_the_app_session(cw):
    _signup(cw)
    cw.http.cookies.clear()
    # no token at all: refused before anything else
    assert hosted_json(cw, "passkey-options", {"purpose": "register", "pool": cw.cpool["id"]}).status_code == 403
    # this browser's own page token, but no app session
    cw.http.get(f"/oidc/{cw.org}/account", params={"pool": cw.cpool["id"]})
    r = hosted_json(cw, "passkey-options", {"purpose": "register", "pool": cw.cpool["id"]})
    assert r.status_code == 401


# ---- C6 CSRF ---------------------------------------------------------------------------------------

def test_c6_hosted_steps_refuse_another_origin_and_json_steps_refuse_forms(cw):
    r, _, _ = _authorize(cw)
    interaction = field_of(r.text, "interaction")
    form = {"interaction": interaction, "email": "a@example.test", "password": STRONG_PW}
    assert hosted(cw, "signin", form, Origin="https://evil.test").status_code == 403
    assert hosted(cw, "signin", form, **{"Sec-Fetch-Site": "cross-site"}).status_code == 403
    r = cw.http.post(f"/oidc/{cw.org}/hosted/passkey-options", content="purpose=login",
                     headers={"Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 403


def test_c6_account_actions_need_the_session_token(cw):
    page, _s, params, verifier = _signup(cw)
    _finish(cw, page, params, verifier)
    acct = _account_csrf(cw, cw.cpool)
    forged = hosted(cw, "account", {"pool": cw.cpool["id"], "action": "revoke", "client_id": cw.cclient["id"],
                                    "csrf": "forged"})
    assert forged.status_code == 403
    token = field_of(acct.text, "csrf")
    ok = hosted(cw, "account", {"pool": cw.cpool["id"], "action": "revoke", "client_id": cw.cclient["id"], "csrf": token})
    assert ok.status_code == 200 and "disconnected" in ok.text


def test_c6_hosted_pages_cannot_be_framed_and_allow_only_their_own_script(cw):
    r, _, _ = _authorize(cw)
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "script-src 'nonce-" in csp and "unsafe" not in csp
    assert r.headers["x-frame-options"] == "DENY"


# ---- C7 disable, reset, revoke -----------------------------------------------------------------------

def test_c7_revoking_consent_on_the_account_page_ends_the_apps_refresh(cw):
    page, _s, params, verifier = _signup(cw)
    tok = _finish(cw, page, params, verifier)
    acct = _account_csrf(cw, cw.cpool)
    hosted(cw, "account", {"pool": cw.cpool["id"], "action": "revoke", "client_id": cw.cclient["id"],
                           "csrf": field_of(acct.text, "csrf")})
    r = cw.token({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}, client=cw.cclient,
                 secret=cw.csecret)
    assert r.json()["error"] == "invalid_grant"
    assert cw.userinfo(tok["access_token"]).status_code == 401


def test_c7_disabling_a_pool_user_kills_sessions_and_tokens(cw):
    page, secret, params, verifier = _signup(cw)
    tok = _finish(cw, page, params, verifier)
    from fastapi.testclient import TestClient

    from apps.api.app import main

    owner = cw.ids.person("owner")
    cw.ids.bind(owner, "owner")
    api = TestClient(main.app, raise_server_exceptions=False)
    user = _user(cw, "cara@example.test")
    with cw.ids.acting_as(cw.ids.ctx(owner)):
        r = api.patch(f"/v1/oidc/pools/{cw.cpool['id']}/users/{user['id']}", json={"status": "disabled"})
        assert r.status_code == 200 and r.json()["status"] == "disabled"
    assert cw.userinfo(tok["access_token"]).status_code == 401
    assert cw.token({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}, client=cw.cclient,
                    secret=cw.csecret).json()["error"] == "invalid_grant"
    r, _ = cw.authorize(cw.cclient["id"], extra={"prompt": "none", "scope": "openid"})
    assert query_of(r.headers["location"])["error"] == "login_required"


def test_c7_password_reset_by_emailed_code_ends_old_sessions(cw):
    page, secret, params, verifier = _signup(cw)
    _finish(cw, page, params, verifier)
    old = cw.http.cookies.get("__Host-boss_app_session", domain="boss.oidc.test")
    r, _, _ = _authorize(cw)
    interaction = None
    cw.http.get(f"/oidc/{cw.org}/account", params={"view": "forgot", "pool": cw.cpool["id"]})
    forgot = hosted(cw, "forgot", {"pool": cw.cpool["id"], "email": "cara@example.test"})
    assert forgot.status_code == 200
    r = hosted(cw, "reset", {"pool": cw.cpool["id"], "code": last_code(cw.mail), "password": "new-" + STRONG_PW})
    assert r.status_code == 200 and "Password changed" in r.text
    with begin() as db:
        assert db.execute(text("SELECT count(*) FROM oidc_sessions WHERE revoked_at IS NULL AND pool_user_id = "
                               ":u"), {"u": _user(cw, "cara@example.test")["id"]}).scalar() == 0
    assert old and interaction is None
    unknown = hosted(cw, "forgot", {"pool": cw.cpool["id"], "email": "nobody@example.test"})
    assert unknown.status_code == 200 and unknown.text.count("If the address has an account") == 1


def test_c7_admin_invites_and_resets_mfa(cw):
    from fastapi.testclient import TestClient

    from apps.api.app import main

    owner = cw.ids.person("owner")
    cw.ids.bind(owner, "owner")
    api = TestClient(main.app, raise_server_exceptions=False)
    with cw.ids.acting_as(cw.ids.ctx(owner)):
        r = api.post(f"/v1/oidc/pools/{cw.cpool['id']}/users", json={"email": "ivy@example.test", "name": "Ivy"})
        assert r.status_code == 201 and r.json()["has_password"] is False
        assert cw.mail[-1][0] == "ivy@example.test"
        assert api.post(f"/v1/oidc/pools/{cw.cpool['id']}/users", json={"email": "ivy@example.test"}).status_code == 400
    page, _s, params, verifier = _signup(cw)
    _finish(cw, page, params, verifier)
    user = _user(cw, "cara@example.test")
    with cw.ids.acting_as(cw.ids.ctx(owner)):
        r = api.patch(f"/v1/oidc/pools/{cw.cpool['id']}/users/{user['id']}", json={"reset_mfa": True})
        assert r.status_code == 200 and r.json()["factors"] == []
    viewer = cw.ids.person("viewer")
    cw.ids.bind(viewer, "viewer")
    with cw.ids.acting_as(cw.ids.ctx(viewer)):
        assert api.get(f"/v1/oidc/pools/{cw.cpool['id']}/users").status_code == 403


# ---- C8 fixation -------------------------------------------------------------------------------------

def test_c8_every_sign_in_makes_a_new_session_and_ends_the_one_it_replaces(cw):
    page, secret, params, verifier = _signup(cw)
    _finish(cw, page, params, verifier)
    planted = cw.http.cookies.get("__Host-boss_app_session", domain="boss.oidc.test")
    r, _p, _v = _sign_in(cw, "cara@example.test", secret, offset=1, prompt="login")
    assert r.status_code == 303
    fresh = cw.http.cookies.get("__Host-boss_app_session", domain="boss.oidc.test")
    assert fresh and fresh != planted
    from boss_engine.oidc_provider import customers

    with begin() as db:
        assert customers.session_lookup(db, pool_id=cw.cpool["id"], token=planted) is None
        assert customers.session_lookup(db, pool_id=cw.cpool["id"], token=fresh) is not None
