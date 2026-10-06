"""Stream S1: password sign-in, sessions, CSRF, lockout and anti-enumeration through the real app.

Adversarial ids (IDENTITY.md §15.3): A1, A2, A3, A7, A8, A9, A16, A18, A19, A20, plus session
expiry, revocation and the MeResponse contract. Each test fails if its protection is removed.
"""
from __future__ import annotations

import statistics
import time

from sqlalchemy import text

from apps.api.tests.identity_s1_world import (  # noqa: F401  (world is a fixture)
    HEADERS,
    ORIGIN,
    STRONG,
    csrf,
    fresh_client,
    make_user,
    set_cookie_values,
    sign_in,
    world,
)
from boss_engine.identity import authn

#: A CSRF failure is an AuthnError("csrf_failed", 403), and the guard keeps the 403 (S1-1, applied
#: at integration). The request is refused before the handler runs.
REFUSED = (403,)


def _session_cookie(client) -> str:
    return client.cookies.get(authn.SESSION_COOKIE)


def test_a_person_signs_in_and_me_answers_the_frozen_contract(world, engine):
    make_user(engine, "ann@example.test", role="operator", name="Ann")
    client = fresh_client()
    res = sign_in(client, "ann@example.test")
    assert res.status_code == 200, res.text
    assert res.json()["next"] == "done"
    assert authn.SESSION_TOKEN_PREFIX not in res.text            # A15: never in a body
    me = client.get("/v1/identity/me")
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["email"] == "ann@example.test" and body["access_roles"] == ["operator"]
    assert body["csrf_token"] and body["session_id"].startswith("ses_")
    assert [e.event_type for e in world.events][-2:] == ["auth.login.succeeded", "auth.new_device"]


def test_a19_cookies_are_host_prefixed_secure_httponly_lax(world, engine):
    make_user(engine, "bea@example.test")
    res = sign_in(fresh_client(), "bea@example.test")
    cookies = set_cookie_values(res)
    assert cookies, "a sign-in sets cookies"
    for c in cookies:
        name = c.split("=", 1)[0]
        assert name.startswith("__Host-"), c
        attrs = {a.strip().lower() for a in c.split(";")[1:]}
        assert {"secure", "httponly", "samesite=lax", "path=/"} <= attrs, c
        assert not any(a.startswith("domain") for a in attrs), c


def test_a20_no_session_cookie_for_a_plain_http_public_origin(world, engine, monkeypatch):
    from boss_engine.config import get_settings

    make_user(engine, "cal@example.test")
    monkeypatch.setattr(get_settings(), "public_url", "http://boss.example.test")
    client = fresh_client()
    res = client.post("/v1/auth/login", json={"email": "cal@example.test", "password": STRONG},
                      headers={"Origin": "http://boss.example.test"})
    assert res.status_code == 400 and res.json() == {"error": "insecure_origin"}
    assert not any(c.startswith(authn.SESSION_COOKIE) for c in set_cookie_values(res))
    monkeypatch.setattr(get_settings(), "public_url", "http://localhost:3000")
    res = client.post("/v1/auth/login", json={"email": "cal@example.test", "password": STRONG},
                      headers={"Origin": "http://localhost:3000"})
    assert res.status_code == 200                                     # loopback is the exception


def _issued(response, name: str) -> str:
    for c in set_cookie_values(response):
        if c.startswith(name + "="):
            return c.split(";", 1)[0].split("=", 1)[1]
    return ""


def test_a7_fixation_the_preauth_cookie_never_becomes_the_session(world, engine):
    make_user(engine, "dee@example.test")
    client = fresh_client()
    client.cookies.set(authn.SESSION_COOKIE, "bss_attacker-chosen-value")
    client.cookies.set(authn.PREAUTH_COOKIE, "bpa_attacker-chosen-value")
    first = client.post("/v1/auth/login", json={"email": "dee@example.test", "password": STRONG},
                        headers=HEADERS)
    assert first.status_code == 200
    issued = _issued(first, authn.SESSION_COOKIE)
    assert issued.startswith("bss_") and issued != "bss_attacker-chosen-value"
    # the attacker-chosen value authenticates nothing
    planted = fresh_client()
    planted.cookies.set(authn.SESSION_COOKIE, "bss_attacker-chosen-value")
    assert planted.get("/v1/identity/me").status_code == 401
    # the issued session works until it is revoked (rotation revokes the old row the same way)
    holder = fresh_client()
    holder.cookies.set(authn.SESSION_COOKIE, issued)
    assert holder.get("/v1/identity/me").status_code == 200
    with engine.begin() as db:
        db.execute(text("UPDATE identity_sessions SET revoked_at = now() WHERE token_hash = :h"),
                   {"h": authn.hash_token(issued)})
    assert holder.get("/v1/identity/me").status_code == 401


def test_a8_csrf_cross_site_origin_and_missing_header_are_refused(world, engine):
    make_user(engine, "eve@example.test")
    client = fresh_client()
    assert sign_in(client, "eve@example.test").status_code == 200
    good = csrf(client)
    # a cookie-authenticated write with no CSRF header
    assert client.delete("/v1/identity/me/sessions/ses_x", headers=HEADERS).status_code in REFUSED
    # from another origin, even with the right header
    bad_origin = {**good, "Origin": "https://evil.example"}
    assert client.delete("/v1/identity/me/sessions/ses_x", headers=bad_origin).status_code in REFUSED
    # Sec-Fetch-Site cross-site
    cross = {**good, "Sec-Fetch-Site": "cross-site"}
    assert client.delete("/v1/identity/me/sessions/ses_x", headers=cross).status_code in REFUSED
    # a forged token
    forged = {**good, "X-Boss-CSRF": good["X-Boss-CSRF"][:-2] + "AA"}
    assert client.delete("/v1/identity/me/sessions/ses_x", headers=forged).status_code in REFUSED
    # all three right: reaches the handler (404 for an unknown session)
    assert client.delete("/v1/identity/me/sessions/ses_x", headers=good).status_code == 404
    # the precise answer of the CSRF check itself
    with engine.begin() as db:
        sid = client.get("/v1/identity/me").json()["session_id"]
        creds = authn.RequestCredentials("DELETE", "/x", {"origin": "https://evil.example"}, {})
        try:
            authn.check_csrf(creds, session_id=sid, public_origin=ORIGIN)
            raise AssertionError("cross-site origin must fail")
        except authn.AuthnError as exc:
            assert (exc.code, exc.status) == ("csrf_failed", 403)
    # login CSRF: a cross-site sign-in POST is refused before the password is looked at
    other = fresh_client()
    res = other.post("/v1/auth/login", json={"email": "eve@example.test", "password": STRONG},
                     headers={"Origin": "https://evil.example"})
    assert res.status_code == 403


def test_a9_identity_headers_and_body_actor_have_no_effect(world, engine):
    victim = make_user(engine, "fay@example.test", role="owner")
    make_user(engine, "gus@example.test", role="viewer")
    client = fresh_client()
    hostile = {"X-Boss-Principal": victim, "X-User-Id": victim, "X-Boss-Actor": victim}
    assert client.get("/v1/identity/me", headers=hostile).status_code == 401
    assert sign_in(client, "gus@example.test").status_code == 200
    me = client.get("/v1/identity/me?actor=" + victim, headers=hostile).json()
    assert me["principal_id"] != victim and me["email"] == "gus@example.test"


def test_one_credential_per_request_cookie_plus_bearer_is_400(world, engine):
    make_user(engine, "hal@example.test")
    client = fresh_client()
    sign_in(client, "hal@example.test")
    res = client.get("/v1/identity/me", headers={"Authorization": "Bearer " + authn.mint_api_key("pat")})
    # S1-1 (integration): the guard keeps S1's status — 400, not a bare 401
    assert res.status_code == 400 and res.json()["error"] == "ambiguous_credentials"
    with engine.begin() as db:
        creds = authn.RequestCredentials("GET", "/v1/identity/me", {"authorization": "Bearer x"},
                                         {authn.SESSION_COOKIE: _session_cookie(client)})
        try:
            authn.authenticate(creds, db=db)
            raise AssertionError("cookie + bearer must not authenticate")
        except authn.AuthnError as exc:
            assert (exc.code, exc.status) == ("ambiguous_credentials", 400)


def test_a3_unknown_and_wrong_password_are_identical_and_similar_in_time(world, engine):
    make_user(engine, "ida@example.test")
    client = fresh_client()
    unknown, wrong = [], []
    bodies = set()
    for i in range(7):
        for bucket, email in ((unknown, f"nobody{i}@example.test"), (wrong, "ida@example.test")):
            started = time.perf_counter()
            res = client.post("/v1/auth/login", json={"email": email, "password": f"wrong-{i}-xxxxxxx"},
                              headers={**HEADERS, "X-Forwarded-For": f"10.9.{i}.{len(bucket)}"})
            bucket.append(time.perf_counter() - started)
            bodies.add((res.status_code, res.content))
        with engine.begin() as db:   # keep the per-IP limit out of this test
            db.execute(text("DELETE FROM identity_rate_limits"))
    assert bodies == {(401, b'{"error":"invalid_credentials"}')}
    mu, mw = statistics.median(unknown), statistics.median(wrong)
    assert abs(mu - mw) / max(mu, mw) < 0.20, (mu, mw)
    # A79: the typed identifier of an unknown account is stored only as an HMAC
    failed = [e for e in world.events if e.event_type == "auth.login.failed"]
    assert failed and all("nobody" not in str(e.details) for e in failed)


def test_a16_disabled_is_revealed_only_after_the_correct_password(world, engine):
    make_user(engine, "jo@example.test", status="disabled")
    client = fresh_client()
    wrong = client.post("/v1/auth/login", json={"email": "jo@example.test", "password": "not-the-password"},
                        headers=HEADERS)
    assert wrong.status_code == 401 and wrong.json() == {"error": "invalid_credentials"}
    right = sign_in(client, "jo@example.test")
    assert right.status_code == 403 and right.json()["error"] == "account_disabled"
    assert not any(c.startswith(authn.SESSION_COOKIE + "=b") for c in set_cookie_values(right))


def test_a1_stuffing_from_one_ip_is_429_with_retry_after(world, engine):
    client = fresh_client()
    statuses = []
    for i in range(25):
        res = client.post("/v1/auth/login", json={"email": f"x{i}@example.test", "password": "p" * 12},
                          headers=HEADERS)
        statuses.append(res.status_code)
    assert statuses[:20] == [401] * 20
    assert set(statuses[20:]) == {429}
    assert int(res.headers["Retry-After"]) >= 1


def test_a2_soft_lock_spares_the_owners_familiar_device_and_passkeys(world, engine):
    make_user(engine, "kim@example.test", role="owner")
    owner_device = fresh_client()
    assert sign_in(owner_device, "kim@example.test").status_code == 200      # now a familiar device
    attacker = fresh_client()
    for i in range(10):
        attacker.post("/v1/auth/login", json={"email": "kim@example.test", "password": f"guess-{i}-zzzzz"},
                      headers={**HEADERS, "X-Forwarded-For": f"10.1.1.{i}"})
    with engine.begin() as db:
        db.execute(text("DELETE FROM identity_rate_limits WHERE bucket = 'signin.ip'"))
    # the attacker, even with the RIGHT password, is soft-locked out (same uniform body)
    locked = sign_in(attacker, "kim@example.test")
    assert locked.status_code == 401 and locked.json() == {"error": "invalid_credentials"}
    assert world.events_of("auth.locked_soft")
    # the owner's familiar device still signs in
    assert sign_in(owner_device, "kim@example.test").status_code == 200


def test_a18_a_password_change_revokes_every_other_session(world, engine):
    make_user(engine, "lou@example.test")
    first, second = fresh_client(), fresh_client()
    sign_in(first, "lou@example.test")
    sign_in(second, "lou@example.test")
    res = first.post("/v1/identity/me/password", json={"current": STRONG, "new": "a-new-long-passphrase-42"},
                     headers=csrf(first))
    assert res.status_code == 200 and res.json()["other_sessions_revoked"] == 1
    assert first.get("/v1/identity/me").status_code == 200
    assert second.get("/v1/identity/me").status_code == 401


def test_sessions_expire_idle_and_absolute_and_logout_ends_them(world, engine):
    make_user(engine, "max@example.test")
    client = fresh_client()
    sign_in(client, "max@example.test")
    token = _session_cookie(client)
    with engine.begin() as db:
        db.execute(text("UPDATE identity_sessions SET idle_expires_at = now() - interval '1 second' "
                        "WHERE token_hash = :h"), {"h": authn.hash_token(token)})
    assert client.get("/v1/identity/me").status_code == 401
    sign_in(client, "max@example.test")
    token = _session_cookie(client)
    with engine.begin() as db:
        db.execute(text("UPDATE identity_sessions SET absolute_expires_at = now() - interval '1 second' "
                        "WHERE token_hash = :h"), {"h": authn.hash_token(token)})
    assert client.get("/v1/identity/me").status_code == 401
    sign_in(client, "max@example.test")
    assert client.post("/v1/auth/logout", headers=HEADERS).status_code == 200
    client.cookies.set(authn.SESSION_COOKIE, _session_cookie(client) or "")
    assert client.get("/v1/identity/me").status_code == 401


def test_j6_revoking_a_session_signs_that_browser_out_at_its_next_request(world, engine):
    make_user(engine, "ned@example.test")
    laptop, phone = fresh_client(), fresh_client()
    sign_in(laptop, "ned@example.test")
    sign_in(phone, "ned@example.test")
    phone_session = phone.get("/v1/identity/me").json()["session_id"]
    sessions_seen = laptop.get("/v1/identity/me/sessions").json()["sessions"]
    assert {s["id"] for s in sessions_seen} >= {phone_session}
    assert laptop.delete(f"/v1/identity/me/sessions/{phone_session}", headers=csrf(laptop)).status_code == 200
    assert phone.get("/v1/identity/me").status_code == 401
    assert world.events_of("session.revoked")
