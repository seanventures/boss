"""Stream S1: magic link, email code, password reset and self sign-up — everything that goes
through a mailbox (IDENTITY.md §4.7, §4.14, §4.15; D26, D30, D32).

Adversarial ids: A11, A12, A15, A17, A81–A88, A91, A92 (sign-in bodies), J4 (API half).
"""
from __future__ import annotations

import re
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
    set_policy,
    sign_in,
    totp_now,
    world,
)
from boss_engine.identity import authn, email_signin, ids, totp
from boss_engine.identity.declarations import Public, access_of

LINK_RE = re.compile(r"#t=([A-Za-z0-9_\-]+)")
CODE_RE = re.compile(r"\b(\d{6})\b")


def _token_from(body: str) -> str:
    m = LINK_RE.search(body)
    assert m, body
    return m.group(1)


def _code_from(body: str) -> str:
    m = CODE_RE.search(body)
    assert m, body
    return m.group(1)


def _enrol_totp(world, engine, pid: str) -> str:
    secret = totp.generate_secret()
    item = ids.new_id("vlt")
    world.vault[item] = secret
    with engine.begin() as db:
        db.execute(text("INSERT INTO identity_totp (principal_id, vault_item_id, last_step) VALUES (:p, :v, 0)"),
                   {"p": pid, "v": item})
    return secret


def _body_fields(route) -> set[str]:
    field = getattr(route, "body_field", None)
    if field is None:
        return set()
    model = getattr(field, "type_", None) or getattr(getattr(field, "field_info", None), "annotation", None)
    return set(getattr(model, "model_fields", {}) or {})


def test_a17_without_smtp_the_email_methods_do_not_exist(world, engine):
    world.smtp = False
    make_user(engine, "nomail@example.test")
    client = fresh_client()
    for path, body in (("/v1/auth/magic-link", {"email": "nomail@example.test"}),
                       ("/v1/auth/email-code", {"email": "nomail@example.test"}),
                       ("/v1/auth/magic-link/verify", {"token": "x" * 43}),
                       ("/v1/auth/email-code/verify", {"code": "123456"}),
                       ("/v1/auth/signup", {"name": "N", "email": "n@example.test"})):
        res = client.post(path, json=body, headers=HEADERS)
        assert res.status_code == 404, (path, res.text)
    methods = client.get("/v1/auth/options").json()
    assert methods["smtp"] is False and methods["signup"] is False
    assert world.mail == []


def test_a81_a15_no_public_auth_route_ever_returns_a_link_token_code_or_session(world, engine):
    """Crawl every public route of the real app with plausible bodies; collect every secret that
    was minted (mailed links and codes, session cookies) and assert none is in any body or
    Location header."""
    from apps.api.app.main import app

    world.smtp = True
    set_policy(engine, signup_enabled=True)
    make_user(engine, "crawl@example.test")
    client = fresh_client()
    plausible = {"email": "crawl@example.test", "password": STRONG, "code": "123456", "token": "t" * 43,
                 "new_password": "another-long-passphrase-9", "name": "Crawl", "purpose": "login",
                 "credential": {}}
    responses = []
    for route in app.router.routes:
        access = access_of(getattr(route, "endpoint", None))
        if not isinstance(access, Public) or not route.path.startswith(("/v1/auth", "/saml")):
            continue
        if "{" in route.path:
            continue
        for method in sorted(getattr(route, "methods", None) or ()):
            if method == "HEAD":
                continue
            names = _body_fields(route)
            body = {k: v for k, v in plausible.items() if k in names}
            res = client.request(method, route.path, json=body if method != "GET" else None, headers=HEADERS)
            responses.append(res)
    # a full sign-in by every email method, so their real secrets exist
    for kind in ("magic-link", "email-code"):
        client.post(f"/v1/auth/{kind}", json={"email": "crawl@example.test"}, headers=HEADERS)
    secrets_seen = []
    for _to, _subject, body in world.mail:
        secrets_seen += LINK_RE.findall(body) + CODE_RE.findall(body)
    _s, last_code_mail = world.last_mail_to("crawl@example.test")
    res = client.post("/v1/auth/email-code/verify", json={"code": _code_from(last_code_mail)}, headers=HEADERS)
    responses.append(res)
    session = client.cookies.get(authn.SESSION_COOKIE)
    assert session, "the email code signed in"
    secrets_seen.append(session)
    assert secrets_seen
    for res in responses:
        visible = res.text + res.headers.get("location", "")
        for secret in secrets_seen:
            assert secret not in visible, (res.request.url, secret[:4])
        assert "bss_" not in res.text and "boss_pat_" not in res.text


def test_a82_a_magic_link_is_single_use_and_dies_after_ten_minutes(world, engine):
    world.smtp = True
    make_user(engine, "ml@example.test")
    client = fresh_client()
    assert client.post("/v1/auth/magic-link", json={"email": "ml@example.test"}, headers=HEADERS).status_code == 202
    token = _token_from(world.last_mail_to("ml@example.test")[1])
    first = client.post("/v1/auth/magic-link/verify", json={"token": token}, headers=HEADERS)
    assert first.status_code == 200 and first.json()["next"] == "done"
    again = fresh_client()
    again.cookies.set(authn.PREAUTH_COOKIE, client.cookies.get(authn.PREAUTH_COOKIE) or "")
    second = client.post("/v1/auth/magic-link/verify", json={"token": token}, headers=HEADERS)
    assert second.status_code == 401
    # a fresh link, aged past 10 minutes
    later = fresh_client()
    later.post("/v1/auth/magic-link", json={"email": "ml@example.test"}, headers=HEADERS)
    token2 = _token_from(world.last_mail_to("ml@example.test")[1])
    with engine.begin() as db:
        db.execute(text("UPDATE identity_auth_challenges SET expires_at = now() - interval '1 second' "
                        "WHERE kind = 'magic_link' AND consumed_at IS NULL"))
    assert later.post("/v1/auth/magic-link/verify", json={"token": token2}, headers=HEADERS).status_code == 401
    with engine.begin() as db:
        ttl = db.execute(text("SELECT max(expires_at - created_at) FROM identity_auth_challenges "
                              "WHERE kind = 'magic_link'")).scalar()
    assert ttl.total_seconds() <= 601


def test_a83_a_magic_link_opened_in_another_browser_signs_nothing_in(world, engine):
    world.smtp = True
    make_user(engine, "mb@example.test")
    asker, other = fresh_client(), fresh_client()
    asker.post("/v1/auth/magic-link", json={"email": "mb@example.test"}, headers=HEADERS)
    token = _token_from(world.last_mail_to("mb@example.test")[1])
    res = other.post("/v1/auth/magic-link/verify", json={"token": token}, headers=HEADERS)
    assert res.status_code == 401 and res.json()["error"] == "wrong_browser"
    assert "browser where you asked" in res.json()["message"]
    assert other.cookies.get(authn.SESSION_COOKIE) is None
    # it still works where it was asked for (the wrong browser did not burn it)
    assert asker.post("/v1/auth/magic-link/verify", json={"token": token}, headers=HEADERS).status_code == 200


def test_a84_at_rest_there_is_neither_the_link_token_nor_the_code(world, engine):
    world.smtp = True
    make_user(engine, "rest@example.test")
    client = fresh_client()
    client.post("/v1/auth/magic-link", json={"email": "rest@example.test"}, headers=HEADERS)
    token = _token_from(world.last_mail_to("rest@example.test")[1])
    client2 = fresh_client()
    client2.post("/v1/auth/email-code", json={"email": "rest@example.test"}, headers=HEADERS)
    code = _code_from(world.last_mail_to("rest@example.test")[1])
    with engine.begin() as db:
        rows = db.execute(text("SELECT * FROM identity_auth_challenges")).fetchall()
    dump = " ".join(repr(tuple(r)) for r in rows)
    assert token not in dump and code not in dump
    assert authn.hash_token(token) in dump                   # stored only as SHA-256


def test_a85_known_and_unknown_addresses_are_indistinguishable(world, engine, monkeypatch):
    world.smtp = True
    monkeypatch.setattr(email_signin, "UNIFORM_FLOOR_SECONDS", 0.35)
    for i in range(6):
        make_user(engine, f"known{i}@example.test", password=None)
    client = fresh_client()
    for kind in ("magic-link", "email-code"):
        known, unknown, bodies = [], [], set()
        for i in range(6):
            for bucket, email in ((known, f"known{i}@example.test"), (unknown, f"ghost{i}@example.test")):
                started = time.perf_counter()
                res = client.post(f"/v1/auth/{kind}", json={"email": email},
                                  headers={**HEADERS, "X-Forwarded-For": f"10.2.{i}.{len(bucket)}"})
                bucket.append(time.perf_counter() - started)
                bodies.add((res.status_code, res.content))
        with engine.begin() as db:
            db.execute(text("DELETE FROM identity_rate_limits"))
        assert bodies == {(202, b'{"sent":true}')}, bodies
        mk, mu = statistics.median(known), statistics.median(unknown)
        assert abs(mk - mu) / max(mk, mu) < 0.20, (kind, mk, mu)


def test_a86_rate_limits_are_shared_by_both_methods_and_a_code_burns_after_five(world, engine):
    world.smtp = True
    make_user(engine, "rl@example.test")
    client = fresh_client()
    statuses = []
    for i in range(6):
        kind = "magic-link" if i % 2 else "email-code"
        statuses.append(client.post(f"/v1/auth/{kind}", json={"email": "rl@example.test"},
                                    headers={**HEADERS, "X-Forwarded-For": f"10.3.0.{i}"}).status_code)
    assert statuses[:5] == [202] * 5 and statuses[5] == 429
    with engine.begin() as db:
        db.execute(text("DELETE FROM identity_rate_limits"))
    # per IP: 20/h across addresses
    ip_statuses = [client.post("/v1/auth/magic-link", json={"email": f"p{i}@example.test"},
                               headers=HEADERS).status_code for i in range(21)]
    assert ip_statuses[:20] == [202] * 20 and ip_statuses[20] == 429
    with engine.begin() as db:
        db.execute(text("DELETE FROM identity_rate_limits"))
    # five wrong codes burn it; the right code afterwards fails
    fresh = fresh_client()
    fresh.post("/v1/auth/email-code", json={"email": "rl@example.test"}, headers=HEADERS)
    code = _code_from(world.last_mail_to("rl@example.test")[1])
    wrong = f"{(int(code) + 1) % 1000000:06d}"
    for _ in range(5):
        assert fresh.post("/v1/auth/email-code/verify", json={"code": wrong}, headers=HEADERS).status_code == 401
    burned = fresh.post("/v1/auth/email-code/verify", json={"code": code}, headers=HEADERS)
    assert burned.status_code == 401 and fresh.cookies.get(authn.SESSION_COOKIE) is None


def test_a87_an_email_method_never_satisfies_mfa_for_an_owner(world, engine):
    world.smtp = True
    owner = make_user(engine, "boss@example.test", role="owner")
    client = fresh_client()
    client.post("/v1/auth/magic-link", json={"email": "boss@example.test"}, headers=HEADERS)
    token = _token_from(world.last_mail_to("boss@example.test")[1])
    no_factor = client.post("/v1/auth/magic-link/verify", json={"token": token}, headers=HEADERS)
    assert no_factor.status_code == 401 and no_factor.json()["error"] == "second_factor_required"
    assert client.cookies.get(authn.SESSION_COOKIE) is None
    secret = _enrol_totp(world, engine, owner)
    client2 = fresh_client()
    client2.post("/v1/auth/email-code", json={"email": "boss@example.test"}, headers=HEADERS)
    code = _code_from(world.last_mail_to("boss@example.test")[1])
    step = client2.post("/v1/auth/email-code/verify", json={"code": code}, headers=HEADERS)
    assert step.status_code == 200 and step.json()["next"] == "mfa"
    assert client2.cookies.get(authn.SESSION_COOKIE) is None           # no session yet
    done = client2.post("/v1/auth/mfa/totp", json={"code": totp_now(secret)}, headers=HEADERS)
    assert done.status_code == 200 and done.json()["next"] == "done"
    me = client2.get("/v1/identity/me").json()
    assert me["mfa_at"] is not None


def test_a88_the_link_carries_its_token_in_the_fragment_and_is_spent_by_post(world, engine, capsys):
    world.smtp = True
    make_user(engine, "frag@example.test")
    client = fresh_client()
    client.post("/v1/auth/magic-link", json={"email": "frag@example.test"}, headers=HEADERS)
    _subject, body = world.last_mail_to("frag@example.test")
    link = next(w for w in body.split() if w.startswith("https://"))
    assert link.startswith(f"{ORIGIN}/signin/link#t=") and "?" not in link
    token = _token_from(body)
    assert client.get(f"/v1/auth/magic-link/verify?token={token}").status_code == 405
    client.post("/v1/auth/magic-link/verify", json={"token": token}, headers=HEADERS)
    out = capsys.readouterr()
    assert token not in out.out and token not in out.err


def test_a11_reset_links_use_the_public_origin_never_the_host_header(world, engine, monkeypatch):
    world.smtp = True
    make_user(engine, "host@example.test")
    client = fresh_client()
    res = client.post("/v1/auth/password/forgot", json={"email": "host@example.test"},
                      headers={**HEADERS, "Host": "evil.example", "X-Forwarded-Host": "evil.example"})
    assert res.status_code == 202
    _s, body = world.last_mail_to("host@example.test")
    assert f"{ORIGIN}/reset#t=" in body and "evil" not in body


def test_forgot_password_always_answers_202(world, engine):
    world.smtp = True
    make_user(engine, "fp@example.test")
    client = fresh_client()
    a = client.post("/v1/auth/password/forgot", json={"email": "fp@example.test"}, headers=HEADERS)
    b = client.post("/v1/auth/password/forgot", json={"email": "nobody@example.test"}, headers=HEADERS)
    assert (a.status_code, a.content) == (b.status_code, b.content) == (202, b'{"sent":true}')
    world.smtp = False
    c = client.post("/v1/auth/password/forgot", json={"email": "fp@example.test"}, headers=HEADERS)
    assert (c.status_code, c.content) == (202, b'{"sent":true}')


def test_a12_j4_reset_is_single_use_revokes_sessions_keeps_mfa_and_dies_on_password_change(world, engine):
    world.smtp = True
    pid = make_user(engine, "rst@example.test")
    secret = _enrol_totp(world, engine, pid)
    laptop = fresh_client()
    assert sign_in(laptop, "rst@example.test").json()["next"] == "mfa"
    assert laptop.post("/v1/auth/mfa/totp", json={"code": totp_now(secret)}, headers=HEADERS).status_code == 200
    client = fresh_client()
    client.post("/v1/auth/password/forgot", json={"email": "rst@example.test"}, headers=HEADERS)
    token = _token_from(world.last_mail_to("rst@example.test")[1])
    new = "a-brand-new-passphrase-77"
    res = client.post("/v1/auth/password/reset", json={"token": token, "new_password": new}, headers=HEADERS)
    assert res.status_code == 200 and res.json() == {"next": "signin"}
    assert client.cookies.get(authn.SESSION_COOKIE) is None              # no session from a reset
    assert laptop.get("/v1/identity/me").status_code == 401              # other sessions revoked
    again = client.post("/v1/auth/password/reset", json={"token": token, "new_password": "x-" + new},
                        headers=HEADERS)
    assert again.status_code == 400                                      # single use
    nxt = sign_in(fresh_client(), "rst@example.test", new)
    assert nxt.status_code == 200 and nxt.json()["next"] == "mfa"        # the reset bypassed nothing
    # a token issued, then a password change by another route: the token is void
    client.post("/v1/auth/password/forgot", json={"email": "rst@example.test"}, headers=HEADERS)
    stale = _token_from(world.last_mail_to("rst@example.test")[1])
    signed = fresh_client()
    sign_in(signed, "rst@example.test", new)
    signed.post("/v1/auth/mfa/totp", json={"code": totp_now(secret, offset_steps=1)}, headers=HEADERS)
    changed = signed.post("/v1/identity/me/password", json={"current": new, "new": "third-passphrase-long-1"},
                          headers=csrf(signed))
    assert changed.status_code == 200, changed.text
    dead = client.post("/v1/auth/password/reset", json={"token": stale, "new_password": "fourth-passphrase-xx"},
                       headers=HEADERS)
    assert dead.status_code == 400


def test_a91_self_signup_is_off_by_default_and_creates_no_access_when_on(world, engine):
    world.smtp = True
    client = fresh_client()
    off = client.post("/v1/auth/signup", json={"name": "S", "email": "s@allowed.test"}, headers=HEADERS)
    assert off.status_code == 404
    assert client.get("/v1/auth/options").json()["signup"] is False
    set_policy(engine, signup_enabled=True, signup_domains=["allowed.test"])
    assert client.get("/v1/auth/options").json()["signup"] is True
    outside = client.post("/v1/auth/signup", json={"name": "O", "email": "o@other.test"}, headers=HEADERS)
    inside = client.post("/v1/auth/signup", json={"name": "S", "email": "s@allowed.test"}, headers=HEADERS)
    assert (outside.status_code, outside.content) == (inside.status_code, inside.content) == (202, b'{"sent":true}')
    assert all(to != "o@other.test" for to, _s, _b in world.mail)
    token = _token_from(world.last_mail_to("s@allowed.test")[1])
    done = client.post("/v1/auth/signup/verify", json={"token": token, "password": "signup-passphrase-123"},
                       headers=HEADERS)
    assert done.status_code == 200, done.text
    me = client.get("/v1/identity/me").json()
    assert me["access_roles"] == []
    with engine.begin() as db:
        row = db.execute(text("SELECT self_signed_up FROM identity_users WHERE email = 's@allowed.test'")).first()
        assert row and row[0] is True
        assert db.execute(text("SELECT count(*) FROM identity_users WHERE email = 'o@other.test'")).scalar() == 0
        assert db.execute(text("SELECT count(*) FROM identity_role_bindings b JOIN identity_users u ON "
                               "u.principal_id = b.subject_id WHERE u.email = 's@allowed.test'")).scalar() == 0


def test_signup_never_claims_an_existing_address(world, engine):
    world.smtp = True
    set_policy(engine, signup_enabled=True)
    make_user(engine, "taken@example.test")
    client = fresh_client()
    res = client.post("/v1/auth/signup", json={"name": "T", "email": "taken@example.test"}, headers=HEADERS)
    assert res.status_code == 202
    assert not any("Finish creating" in s for to, s, _b in world.mail if to == "taken@example.test")
