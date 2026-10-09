"""BOSS as an OIDC provider: re-authentication of BOSS members (F6 §6.2 prompt/max_age, §6.3
step-up for require_mfa; stream S6 phase 2), through identity's own step-up.

R1 prompt=login asks a signed-in member to confirm, and only a fresh confirmation issues a code ·
R2 max_age · R3 require_mfa wants a second factor, never a password · R4 the password route for a
member with no factor · R5 the steps are CSRF-checked and bound to the member's own session.
"""
from __future__ import annotations

import time

import jwt
import pytest
from sqlalchemy import text

from apps.api.tests.oidc_world import HOSTED, ORIGIN, STRONG_PW, field_of, make_world, pkce, query_of
from boss_engine.db import begin
from boss_engine.identity import totp, vault


@pytest.fixture
def w(engine, monkeypatch):
    return make_world(engine, monkeypatch)


def _with_totp(w, person) -> str:
    secret = totp.generate_secret()
    with begin() as db:
        item = vault.store(org_id=w.org, owner_principal_id=person.principal.id, kind=vault.VaultItemKind.TOTP_SECRET,
                           provider="totp", secret=secret, db=db)
        db.execute(text("INSERT INTO identity_totp (principal_id, vault_item_id, last_step) VALUES (:p, :v, 0)"),
                   {"p": person.principal.id, "v": item})
    return secret


def _with_password(person) -> None:
    from boss_engine.identity.passwords import hash_password

    with begin() as db:
        db.execute(text("UPDATE identity_users SET password_hash = :h WHERE principal_id = :p"),
                   {"h": hash_password(STRONG_PW), "p": person.principal.id})


def _reauth(w, person, form, **headers):
    from urllib.parse import urlencode

    h = {**HOSTED, "Cookie": f"__Host-boss_session={person.cookie}", **headers}
    return w.http.post(f"/oidc/{w.org}/hosted/wf-reauth", content=urlencode(form), headers=h, follow_redirects=False)


def _new_console_cookie(r) -> str:
    for c in r.headers.get_list("set-cookie"):
        if c.startswith("__Host-boss_session="):
            return c.split(";", 1)[0].split("=", 1)[1]
    raise AssertionError("no console session cookie")


def _ready(w, name="alice", **client_over):
    person = w.person(name)
    client, secret = w.client(first_party=True, **client_over)
    return person, client, secret


def test_r1_prompt_login_asks_to_confirm_and_a_fresh_totp_issues_the_code(w):
    alice, client, secret = _ready(w)
    seed = _with_totp(w, alice)
    verifier, challenge = pkce()
    r, params = w.authorize(client["id"], who=alice, challenge=challenge,
                            extra={"prompt": "login", "scope": "openid email"})
    assert r.status_code == 200 and "Confirm it" in r.text and "location" not in r.headers
    interaction = field_of(r.text, "interaction")
    bad = _reauth(w, alice, {"interaction": interaction, "code": "000000"})
    assert bad.status_code == 401 and "Confirm it" in bad.text
    ok = _reauth(w, alice, {"interaction": interaction, "code": totp.code_at(seed, totp.current_step())})
    assert ok.status_code == 303, ok.text[:300]
    q = query_of(ok.headers["location"])
    assert q["iss"] == w.issuer and q["state"] == params["state"]
    rotated = _new_console_cookie(ok)
    assert rotated != alice.cookie
    with begin() as db:
        assert db.execute(text("SELECT revoked_at IS NOT NULL FROM identity_sessions WHERE id = :s"),
                          {"s": alice.session_id}).scalar() is True          # step-up rotated the session
    tok = w.exchange(client, secret, q["code"], verifier).json()
    claims = jwt.decode(tok["id_token"], options={"verify_signature": False})
    assert int(time.time()) - claims["auth_time"] < 30 and claims["acr"] == "urn:boss:acr:mfa"
    again = _reauth(w, alice, {"interaction": interaction, "code": totp.code_at(seed, totp.current_step() + 1)})
    assert again.status_code in (400, 401)                                  # the request was spent


def test_r1_the_same_totp_code_cannot_confirm_twice(w):
    alice, client, _ = _ready(w)
    seed = _with_totp(w, alice)
    code = totp.code_at(seed, totp.current_step())
    r, _ = w.authorize(client["id"], who=alice, extra={"prompt": "login", "scope": "openid"})
    ok = _reauth(w, alice, {"interaction": field_of(r.text, "interaction"), "code": code})
    assert ok.status_code == 303
    alice.cookie = _new_console_cookie(ok)
    r, _ = w.authorize(client["id"], who=alice, extra={"prompt": "login", "scope": "openid"})
    assert _reauth(w, alice, {"interaction": field_of(r.text, "interaction"), "code": code}).status_code == 401


def test_r2_max_age(w):
    alice, client, _ = _ready(w)
    with begin() as db:
        db.execute(text("UPDATE identity_sessions SET created_at = now() - interval '2 hours', "
                        "mfa_at = now() - interval '2 hours' WHERE id = :s"), {"s": alice.session_id})
    r, _ = w.authorize(client["id"], who=alice, extra={"max_age": "7200000", "scope": "openid"})
    assert r.status_code == 302 and "code=" in r.headers["location"]
    r, _ = w.authorize(client["id"], who=alice, extra={"max_age": "60", "scope": "openid"})
    assert r.status_code == 200 and "Confirm it" in r.text
    r, _ = w.authorize(client["id"], who=alice, extra={"max_age": "60", "prompt": "none", "scope": "openid"})
    assert query_of(r.headers["location"])["error"] == "login_required"


def test_r3_require_mfa_wants_a_second_factor_never_a_password(w):
    weak = w.person("weak", mfa=False, methods=("password",))
    seed = _with_totp(w, weak)
    _with_password(weak)
    client, secret = w.client(require_mfa=True, first_party=True)
    r, _ = w.authorize(client["id"], who=weak, extra={"scope": "openid"})
    assert r.status_code == 200 and "second factor" in r.text and "password instead" not in r.text
    interaction = field_of(r.text, "interaction")
    pw = _reauth(w, weak, {"interaction": interaction, "password": STRONG_PW})
    assert pw.status_code == 401                                  # a password is not a second factor
    ok = _reauth(w, weak, {"interaction": interaction, "code": totp.code_at(seed, totp.current_step())})
    assert ok.status_code == 303 and "code=" in ok.headers["location"]


def test_r4_a_member_with_no_factor_confirms_with_their_password(w):
    plain = w.person("plain", mfa=False, methods=("password",))
    _with_password(plain)
    client, _ = w.client(first_party=True)
    r, _ = w.authorize(client["id"], who=plain, extra={"prompt": "login", "scope": "openid"})
    interaction = field_of(r.text, "interaction")
    page = w.http.get(f"/oidc/{w.org}/account", params={"view": "wf-password", "interaction": interaction})
    assert page.status_code == 200 and 'type="password"' in page.text
    wrong = _reauth(w, plain, {"interaction": interaction, "password": "not-the-password-1"})
    assert wrong.status_code == 401
    ok = _reauth(w, plain, {"interaction": interaction, "password": STRONG_PW})
    assert ok.status_code == 303 and "code=" in ok.headers["location"]
    claims_acr = "urn:boss:acr:pwd"          # a password step-up is not MFA
    with begin() as db:
        acr = db.execute(text("SELECT acr FROM oidc_grants WHERE client_id = :c ORDER BY created_at DESC LIMIT 1"),
                         {"c": client["id"]}).scalar()
    assert acr == claims_acr


def test_r5_the_confirmation_is_csrf_checked_and_needs_a_session(w):
    alice, client, _ = _ready(w)
    seed = _with_totp(w, alice)
    r, _ = w.authorize(client["id"], who=alice, extra={"prompt": "login", "scope": "openid"})
    interaction = field_of(r.text, "interaction")
    code = totp.code_at(seed, totp.current_step())
    assert _reauth(w, alice, {"interaction": interaction, "code": code}, Origin="https://evil.test").status_code == 403
    assert _reauth(w, alice, {"interaction": interaction, "code": code},
                   **{"Sec-Fetch-Site": "cross-site"}).status_code == 403
    from urllib.parse import urlencode

    anon = w.http.post(f"/oidc/{w.org}/hosted/wf-reauth", content=urlencode({"interaction": interaction, "code": code}),
                       headers=HOSTED, follow_redirects=False)
    assert anon.status_code == 401 and "location" not in anon.headers


_ = ORIGIN
