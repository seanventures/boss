"""The login-oauth template's own tests, against its HTTP routes (not internals), with an
in-process fake identity provider: no network. Run: python3 -m pytest -q tests/template_login_oauth"""
from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, quote, urlsplit

import httpx
from fastapi.testclient import TestClient

from conftest import BASE_URL


class Browser:
    """A browser: its own cookie jar, manual redirects, https as the TLS proxy would report."""

    def __init__(self, app):
        self.client = TestClient(app, base_url=BASE_URL, follow_redirects=False)
        self.set_cookies: list[str] = []

    def get(self, path: str, **kw):
        res = self.client.get(path, **kw)
        self.set_cookies += res.headers.get_list("set-cookie")
        return res

    def post(self, path: str, **kw):
        res = self.client.post(path, **kw)
        self.set_cookies += res.headers.get_list("set-cookie")
        return res

    def to_provider(self, provider: str, query: str = ""):
        start = self.get(f"/auth/login/{provider}{query}")
        assert start.status_code == 302, start.text
        authorize_url = start.headers["location"]
        at_idp = httpx.get(authorize_url, follow_redirects=False)
        assert at_idp.status_code == 302, at_idp.text
        back = urlsplit(at_idp.headers["location"])
        assert f"{back.scheme}://{back.netloc}" == BASE_URL
        return authorize_url, f"{back.path}?{back.query}"

    def login(self, provider: str, query: str = ""):
        _, callback = self.to_provider(provider, query)
        return self.get(callback)


def test_signed_out_me_and_admin_answer_401(app):
    b = Browser(app)
    assert b.get("/api/me").status_code == 401
    assert b.get("/api/me").json() == {"error": "signed_out"}
    assert b.get("/api/admin").status_code == 401


def test_google_login_round_trip_with_the_fake_provider(app):
    b = Browser(app)
    done = b.login("google")
    assert done.status_code == 302 and done.headers["location"] == "/"
    me = b.get("/api/me")
    assert me.status_code == 200
    body = me.json()
    assert body["email"] == "ada@example.com"
    assert body["roles"] == ["user"] and body["providers"] == ["google"] and body["csrf_token"]


def test_github_login_uses_the_verified_primary_email(app, idp):
    idp.user = {"sub": "gh-42", "email": "grace@example.com", "email_verified": True, "name": "grace"}
    b = Browser(app)
    assert b.login("github").status_code == 302
    body = b.get("/api/me").json()
    assert body["email"] == "grace@example.com" and body["email_verified"] is True
    assert body["providers"] == ["github"]


def test_sign_in_with_boss_works_through_oidc_discovery(app):
    b = Browser(app)
    ids = sorted(p["id"] for p in b.get("/auth/providers").json()["providers"])
    assert ids == ["boss", "github", "google"]
    assert b.login("boss").status_code == 302
    assert b.get("/api/me").status_code == 200


def test_a_state_mismatch_is_rejected_and_the_attempt_is_spent(app):
    b = Browser(app)
    _, callback = b.to_provider("google")
    forged = callback.replace("state=", "state=forged", 1)
    res = b.get(forged)
    assert res.status_code == 400 and res.json() == {"error": "invalid_state"}
    assert b.get("/api/me").status_code == 401
    assert b.get(callback).status_code == 400  # the real state cannot be replayed now


def test_a_callback_in_a_browser_that_never_started_the_login_is_rejected(app):
    attacker = Browser(app)
    _, callback = attacker.to_provider("google")
    victim = Browser(app)
    assert victim.get(callback).status_code == 400
    assert victim.get("/api/me").status_code == 401


def test_pkce_s256_is_used(app, idp):
    b = Browser(app)
    authorize_url, callback = b.to_provider("google")
    assert b.get(callback).status_code == 302
    q = {k: v[0] for k, v in parse_qs(urlsplit(authorize_url).query).items()}
    assert q["code_challenge_method"] == "S256"
    verifier = idp.last_token["code_verifier"]
    assert len(verifier) >= 43
    hashed = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert hashed == q["code_challenge"]
    assert b.get("/api/me").status_code == 200


def test_an_id_token_with_another_nonce_is_rejected(app, idp):
    idp.tamper = {"nonce": "a-different-nonce"}
    b = Browser(app)
    res = b.login("google")
    assert res.status_code == 400 and res.json() == {"error": "login_failed"}
    assert b.get("/api/me").status_code == 401


def test_an_unverified_email_does_not_link_to_an_existing_account(app, idp):
    assert Browser(app).login("google").status_code == 302  # ada@example.com, verified
    idp.user = {"sub": "gh-7", "email": "ada@example.com", "email_verified": False, "name": "mallory"}
    attacker = Browser(app)
    res = attacker.login("github")
    assert res.status_code == 409 and res.json() == {"error": "account_exists_unverified_email"}
    assert attacker.get("/api/me").status_code == 401


def test_a_verified_email_links_a_second_provider(app, idp):
    b = Browser(app)
    b.login("google")
    user_id = b.get("/api/me").json()["id"]
    idp.user = {"sub": "gh-8", "email": "ada@example.com", "email_verified": True, "name": "ada"}
    again = Browser(app)
    again.login("github")
    me = again.get("/api/me").json()
    assert me["id"] == user_id and sorted(me["providers"]) == ["github", "google"]


def test_the_session_cookie_flags_and_renewal_at_sign_in(app):
    b = Browser(app)
    _, callback = b.to_provider("google")
    before = b.client.cookies.get("sid")
    b.get(callback)
    after = b.client.cookies.get("sid")
    assert before and after and before != after, "a new session id after sign-in (no fixation)"
    cookie = b.set_cookies[-1]
    assert cookie.startswith("sid=")
    for flag in ("HttpOnly", "Secure", "SameSite=Lax"):
        assert flag.lower() in cookie.lower(), cookie


def test_logout_needs_the_csrf_token_then_clears_the_session(app):
    b = Browser(app)
    b.login("google")
    csrf = b.get("/api/me").json()["csrf_token"]
    assert b.post("/auth/logout").status_code == 403
    assert b.post("/auth/logout", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert b.get("/api/me").status_code == 200, "a refused logout leaves the session"
    assert b.post("/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 204
    assert b.get("/api/me").status_code == 401


def test_the_admin_route_needs_the_admin_role(app, idp):
    user = Browser(app)
    user.login("google")
    assert user.get("/api/admin").status_code == 403
    idp.user = {"sub": "g-boss", "email": "boss@example.com", "email_verified": True, "name": "Boss"}
    admin = Browser(app)
    admin.login("google")
    assert admin.get("/api/me").json()["roles"] == ["user", "admin"]
    assert admin.get("/api/admin").status_code == 200


def test_return_to_only_accepts_a_local_path(app):
    assert Browser(app).login("google", "?return_to=/dashboard").headers["location"] == "/dashboard"
    for evil in ("https://evil.example", "//evil.example", "/\\evil.example"):
        res = Browser(app).login("google", f"?return_to={quote(evil, safe='')}")
        assert res.headers["location"] == "/"


def test_an_unknown_provider_is_a_404(app):
    b = Browser(app)
    assert b.get("/auth/login/facebook").status_code == 404
    assert b.get("/auth/callback/facebook?code=x&state=y").status_code == 404
