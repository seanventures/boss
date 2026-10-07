"""A92 (IDENTITY.md §15.3): after EVERY sign-in method that can be driven in-process, no session or
access token is placed in a redirect URL, a query string, or a response body field a script could
store — and the session cookie is HttpOnly (Kaappu put `oauth_token` in the redirect URL and the
token in localStorage).

Each flow runs on a recording client: every response (and every redirect hop) the browser would see
is kept, and after the flow the session the browser ended up with is looked for everywhere a script
or a URL could see it. Methods: password, password + TOTP, password + recovery code, passkey, magic
link, email code, OIDC SSO callback, the setup claim, invitation accept, and a password reset (which
issues no session; the sign-in after it is checked).

The localStorage half of A92 (nothing written to window.localStorage / sessionStorage after a
sign-in) needs a browser: it is in the FX-D lead's Playwright walk (tests/e2e), not here.
"""
from __future__ import annotations

import re
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

import pytest

from apps.api.tests.identity_s1_world import (  # noqa: F401  (world is a fixture)
    HEADERS,
    ORIGIN,
    STRONG,
    csrf,
    fresh_client,
    make_user,
    promote_fresh_mfa,
    sign_in,
    totp_now,
    world,
)
from apps.api.tests.test_identity_authn_claim import _claim, _setup_token
from apps.api.tests.test_identity_authn_email import _code_from, _token_from
from apps.api.tests.test_identity_authn_mfa import (
    SoftAuthenticator,
    _enrol_totp,
    _passkey_sign_in,
    _register_passkey,
)
from apps.api.tests.test_identity_authn_sso import (  # noqa: F401  (idp is a fixture)
    idp,
    invite,
    login_via,
    make_provider,
)
from boss_engine.identity import authn

#: Names a token must never travel under in a URL or a script-readable JSON field. (`session_id`
#: is not one: it is the session's public `ses_` id, never the bearer value — the value itself is
#: looked for everywhere separately.)
TOKEN_NAMES = re.compile(r"^(token|access_token|id_token|refresh_token|oauth_token|session|"
                         r"session_token|sid|jwt|bearer|auth|authorization)$", re.I)
SESSION_PREFIX = "bss_"


class Recorder:
    """Wraps a TestClient so every response it receives (redirect hops included) is kept."""

    def __init__(self, client: Any):
        self.client = client
        self.responses: list[Any] = []
        original: Callable[..., Any] = client.request

        def request(*args: Any, **kwargs: Any) -> Any:
            res = original(*args, **kwargs)
            self.responses.extend(list(res.history) + [res])
            return res
        client.request = request

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)


def _json_token_fields(value: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            if TOKEN_NAMES.match(str(key)) and isinstance(inner, str) and inner:
                found.append(f"{path}.{key}")
            found += _json_token_fields(inner, f"{path}.{key}")
    elif isinstance(value, list):
        for i, inner in enumerate(value):
            found += _json_token_fields(inner, f"{path}[{i}]")
    return found


def assert_no_token_leaves(rec: Recorder, *, expect_session: bool = True) -> None:
    session = rec.client.cookies.get(authn.SESSION_COOKIE)
    if expect_session:
        assert session and session.startswith(SESSION_PREFIX), "the flow signed in"
    problems: list[str] = []
    for res in rec.responses:
        where = f"{res.request.method} {urlsplit(str(res.request.url)).path} -> {res.status_code}"
        location = res.headers.get("location", "")
        if location:
            parts = urlsplit(location)
            for name, val in parse_qsl(parts.query, keep_blank_values=True) + \
                    parse_qsl(parts.fragment, keep_blank_values=True):
                if TOKEN_NAMES.match(name):
                    problems.append(f"{where}: Location carries {name}=")
                if val.startswith(SESSION_PREFIX):
                    problems.append(f"{where}: Location carries a session-shaped value under {name}")
            if session and session in location:
                problems.append(f"{where}: the session is in the Location")
            if SESSION_PREFIX in location:
                problems.append(f"{where}: a session-shaped value is in the Location")
        body = res.text
        if session and session in body:
            problems.append(f"{where}: the session is in the body")
        if SESSION_PREFIX in body:
            problems.append(f"{where}: a session-shaped value is in the body")
        if "application/json" in res.headers.get("content-type", ""):
            try:
                fields = _json_token_fields(res.json())
            except ValueError:
                fields = []
            if fields:
                problems.append(f"{where}: JSON token fields {fields}")
        for header, value in res.headers.items():
            if header.lower() != "set-cookie" and session and session in value:
                problems.append(f"{where}: the session is in header {header}")
        for cookie in res.headers.get_list("set-cookie"):
            value = cookie.split(";", 1)[0].split("=", 1)[1] if "=" in cookie.split(";", 1)[0] else ""
            if cookie.startswith(authn.SESSION_COOKIE + "=") and value.strip('"'):
                flags = {f.strip().lower().split("=")[0] for f in cookie.split(";")[1:]}
                if not {"httponly", "secure", "samesite", "path"} <= flags:
                    problems.append(f"{where}: the session cookie lacks a flag: {sorted(flags)}")
    assert not problems, "\n".join(problems)


def _rec() -> Recorder:
    return Recorder(fresh_client())


# ---- the methods -----------------------------------------------------------------------------------------

def test_a92_password(world, engine):
    make_user(engine, "pw@example.test")
    rec = _rec()
    assert sign_in(rec, "pw@example.test").json()["next"] == "done"
    rec.get("/v1/identity/me")
    assert_no_token_leaves(rec)


def test_a92_password_then_totp_and_password_then_recovery_code(world, engine):
    make_user(engine, "totp@example.test")
    enrol = fresh_client()
    sign_in(enrol, "totp@example.test")
    secret, codes = _enrol_totp(enrol)
    assert codes
    rec = _rec()
    assert sign_in(rec, "totp@example.test").json()["next"] == "mfa"
    done = rec.post("/v1/auth/mfa/totp", json={"code": totp_now(secret, offset_steps=1)}, headers=HEADERS)
    assert done.status_code == 200, done.text
    rec.get("/v1/identity/me")
    assert_no_token_leaves(rec)
    rec2 = _rec()
    sign_in(rec2, "totp@example.test")
    assert rec2.post("/v1/auth/mfa/recovery", json={"code": codes[0]}, headers=HEADERS).status_code == 200
    assert_no_token_leaves(rec2)


def test_a92_passkey(world, engine):
    pid = make_user(engine, "pk@example.test")
    enrol = fresh_client()
    sign_in(enrol, "pk@example.test")
    auth = SoftAuthenticator(pid.encode())
    assert _register_passkey(enrol, auth).status_code == 201
    rec = _rec()
    res = _passkey_sign_in(rec, auth)
    assert res.status_code == 200 and res.json()["next"] == "done", res.text
    rec.get("/v1/identity/me")
    assert_no_token_leaves(rec)


@pytest.mark.parametrize("method", ["magic-link", "email-code"])
def test_a92_email_methods(world, engine, method):
    world.smtp = True
    make_user(engine, "mail@example.test")
    rec = _rec()
    assert rec.post(f"/v1/auth/{method}", json={"email": "mail@example.test"}, headers=HEADERS).status_code == 202
    mail = world.last_mail_to("mail@example.test")[1]
    if method == "magic-link":
        res = rec.post("/v1/auth/magic-link/verify", json={"token": _token_from(mail)}, headers=HEADERS)
    else:
        res = rec.post("/v1/auth/email-code/verify", json={"code": _code_from(mail)}, headers=HEADERS)
    assert res.status_code == 200, res.text
    assert_no_token_leaves(rec)


def test_a92_oidc_sso_callback(world, engine, idp):
    provider = make_provider(engine)
    invite(engine, "pat@corp.test")
    rec = _rec()
    res, _ = login_via(rec, provider, idp)
    assert res.status_code == 303 and res.headers["location"] == f"{ORIGIN}/", res.headers.get("location")
    rec.get("/v1/identity/me")
    assert_no_token_leaves(rec)


def test_a92_setup_claim(world, engine, capsys):
    token = _setup_token(capsys)
    rec = _rec()
    res = _claim(rec, setup_token=token)
    assert res.status_code == 200, res.text
    rec.get("/v1/identity/me")
    assert_no_token_leaves(rec)


def test_a92_invitation_accept(world, engine):
    make_user(engine, "inviter@example.test", role="owner")
    owner = fresh_client()
    sign_in(owner, "inviter@example.test")
    promote_fresh_mfa(engine, owner.cookies.get(authn.SESSION_COOKIE))
    made = owner.post("/v1/identity/users", headers=csrf(owner), json={
        "email": "newcomer@example.test", "bindings": [{"role_key": "viewer", "scope_type": "org"}]})
    assert made.status_code == 201, made.text
    token = made.json()["link"].split("#t=", 1)[1]
    rec = _rec()
    res = rec.post("/v1/auth/invitations/accept", headers=HEADERS,
                   json={"token": token, "name": "New", "password": STRONG})
    assert res.status_code == 200, res.text
    rec.get("/v1/identity/me")
    assert_no_token_leaves(rec)


def test_a92_password_reset_and_the_sign_in_after_it(world, engine):
    world.smtp = True
    make_user(engine, "reset@example.test")
    rec = _rec()
    rec.post("/v1/auth/password/forgot", json={"email": "reset@example.test"}, headers=HEADERS)
    token = _token_from(world.last_mail_to("reset@example.test")[1])
    new = "a-different-long-passphrase-4"
    res = rec.post("/v1/auth/password/reset", json={"token": token, "new_password": new}, headers=HEADERS)
    assert res.status_code == 200, res.text
    assert_no_token_leaves(rec, expect_session=False)     # a reset issues no session at all
    assert rec.cookies.get(authn.SESSION_COOKIE) is None
    sign_in(rec, "reset@example.test", new)
    assert_no_token_leaves(rec)


def test_the_recorder_would_catch_a_token_in_a_location():
    """The check itself: a Location carrying ?access_token= or a bss_ value is reported."""
    class FakeRes:
        def __init__(self, location, body=""):
            self.headers = _Headers({"location": location, "content-type": "text/plain"})
            self.text = body
            self.status_code = 303
            self.history = []
            self.request = type("R", (), {"method": "GET", "url": "https://x.test/cb"})()

    class _Headers(dict):
        def get_list(self, _name):
            return []

    class FakeClient:
        cookies = {authn.SESSION_COOKIE: "bss_abc"}

        def request(self, *a, **k):
            return None

    rec = Recorder(FakeClient())
    rec.responses = [FakeRes("https://boss.example.test/?access_token=xyz")]
    with pytest.raises(AssertionError, match="access_token"):
        assert_no_token_leaves(rec)
    rec.responses = [FakeRes("https://boss.example.test/#s=bss_abc")]
    with pytest.raises(AssertionError, match="session"):
        assert_no_token_leaves(rec)
