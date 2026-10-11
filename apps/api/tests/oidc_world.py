"""The OIDC provider's test world (F6, stream S6): a real org, real people with real console
sessions, a workforce pool and clients, driven through the real app. Never import from product code.
"""
from __future__ import annotations

import base64
import re
import hashlib
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urlsplit

import jwt
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.db import begin
from boss_engine.identity import sessions, vault
from boss_engine.oidc_provider import clients as oidc_clients
from boss_engine.oidc_provider import keys as oidc_keys
from boss_engine.oidc_provider import pools as oidc_pools

ORIGIN = "https://boss.oidc.test"
APP = "https://app.example.test"
REDIRECT = f"{APP}/callback"
LOGOUT = f"{APP}/bye"


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def pkce() -> tuple[str, str]:
    verifier = b64u(secrets.token_bytes(32))
    return verifier, b64u(hashlib.sha256(verifier.encode()).digest())


@dataclass
class Person:
    principal: Any
    cookie: str
    session_id: str


@dataclass
class OidcWorld:
    ids: IdentityWorld
    http: TestClient
    issuer: str
    pool: dict[str, Any]
    people: dict[str, Person] = field(default_factory=dict)

    @property
    def org(self) -> str:
        return self.ids.org

    # ---- setup ------------------------------------------------------------------------------
    def person(self, name: str, *, role: Optional[str] = "viewer", project: Optional[str] = "a",
               mfa: bool = True, methods: tuple[str, ...] = ("password", "totp")) -> Person:
        p = self.ids.person(name)
        with begin() as db:
            db.execute(text("INSERT INTO identity_users (principal_id, email, email_verified_at) VALUES "
                            "(:p, :e, now())"), {"p": p.id, "e": f"{name}-{uuid.uuid4().hex[:6]}@example.test"})
            issued = sessions.issue(db, principal_id=p.id, org_id=self.org, methods=methods,
                                    mfa_at=datetime.now(timezone.utc) if mfa else None, ip="127.0.0.1",
                                    user_agent="pytest", device_id_hash=None)
        if role:
            self.ids.bind(p, role, project={"a": self.ids.project_a, "b": self.ids.project_b}.get(project or ""))
        person = Person(p, issued.token, issued.session_id)
        self.people[name] = person
        return person

    def client(self, **over: Any) -> tuple[dict[str, Any], Optional[str]]:
        body = {"project_id": self.ids.project_a, "app_key": "bank", "pool_id": self.pool["id"], "name": "Bank",
                "type": "confidential", "redirect_uris": [REDIRECT], "post_logout_redirect_uris": [LOGOUT],
                "allowed_scopes": ["openid", "profile", "email", "roles", "offline_access"]}
        body.update(over)
        with begin() as db:
            return oidc_clients.create(db, org_id=self.org, actor="usr_test", body=body)

    def cookie(self, who: Person) -> str:
        """The Cookie header of this browser signed in as `who`: their console session, plus the
        pre-auth cookie the hosted pages bound their form tokens to (as a browser would send)."""
        pre = self.http.cookies.get("__Host-boss_app_preauth", domain=urlsplit(ORIGIN).hostname)
        return f"__Host-boss_session={who.cookie}" + (f"; __Host-boss_app_preauth={pre}" if pre else "")

    # ---- the protocol -------------------------------------------------------------------------
    def authorize(self, client_id: str, *, who: Optional[Person] = None, extra: Optional[dict[str, Any]] = None,
                  drop: tuple[str, ...] = (), challenge: Optional[str] = None, redirect_uri: str = REDIRECT,
                  raw_query: Optional[str] = None):
        params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
                  "scope": "openid profile email roles offline_access", "state": "st-" + secrets.token_hex(4),
                  "nonce": "n-" + secrets.token_hex(6), "code_challenge": challenge or pkce()[1],
                  "code_challenge_method": "S256"}
        params.update(extra or {})
        for d in drop:
            params.pop(d, None)
        query = raw_query if raw_query is not None else urlencode(params)
        headers = {"Cookie": self.cookie(who)} if who else {}
        return self.http.get(f"/oidc/{self.org}/authorize?{query}", headers=headers, follow_redirects=False), params

    def consent(self, page: Any, who: Person, *, decision: str = "allow", origin: Optional[str] = ORIGIN,
                fetch_site: Optional[str] = "same-origin", csrf: Optional[str] = None,
                interaction: Optional[str] = None):
        html = page.text
        form = {"interaction": interaction or _field(html, "interaction"), "csrf": csrf or _field(html, "csrf"),
                "decision": decision}
        headers = {"Cookie": f"__Host-boss_session={who.cookie}",
                   "Content-Type": "application/x-www-form-urlencoded"}
        if origin:
            headers["Origin"] = origin
        if fetch_site:
            headers["Sec-Fetch-Site"] = fetch_site
        return self.http.post(f"/oidc/{self.org}/consent", content=urlencode(form), headers=headers,
                              follow_redirects=False)

    def sign_in(self, client: dict[str, Any], who: Person, *, scope: Optional[str] = None
                ) -> tuple[str, str, dict[str, Any]]:
        """authorize (+ consent when asked) -> (code, verifier, authorize params)."""
        verifier, challenge = pkce()
        extra = {"scope": scope} if scope else {}
        r, params = self.authorize(client["id"], who=who, challenge=challenge, extra=extra)
        if r.status_code == 200:
            r = self.consent(r, who)
        assert r.status_code in (302, 303), (r.status_code, r.text[:300])
        q = query_of(r.headers["location"])
        assert q.get("iss") == self.issuer and q.get("state") == params["state"], q
        return q["code"], verifier, params

    def token(self, form: dict[str, Any], *, client: Optional[dict[str, Any]] = None, secret: Optional[str] = None,
              headers: Optional[dict[str, str]] = None, issuer_org: Optional[str] = None):
        h = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
        if client is not None and secret is not None:
            h["Authorization"] = "Basic " + base64.b64encode(f"{client['id']}:{secret}".encode()).decode()
        return self.http.post(f"/oidc/{issuer_org or self.org}/token", content=urlencode(form), headers=h)

    def exchange(self, client: dict[str, Any], secret: Optional[str], code: str, verifier: str, **over: Any):
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "code_verifier": verifier}
        form.update(over)
        if secret is None:
            form.setdefault("client_id", client["id"])
        return self.token({k: v for k, v in form.items() if v is not None}, client=client, secret=secret)

    def userinfo(self, access_token: str):
        return self.http.get(f"/oidc/{self.org}/userinfo", headers={"Authorization": f"Bearer {access_token}"})

    def jwks(self) -> dict[str, Any]:
        return self.http.get(f"/oidc/{self.org}/jwks").json()

    def verify(self, token: str, *, audience: str, typ: str = "JWT") -> dict[str, Any]:
        header = jwt.get_unverified_header(token)
        assert header["typ"] == typ
        key = next(k for k in self.jwks()["keys"] if k["kid"] == header["kid"])
        return jwt.decode(token, key=jwt.PyJWK(key).key, algorithms=[key["alg"]], audience=audience,
                          issuer=self.issuer)


def _field(html: str, name: str) -> str:
    import re

    m = re.search(rf'name="{name}" value="([^"]+)"', html)
    assert m, f"no {name} field in page"
    return m.group(1)


def query_of(location: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


def make_world(engine: Any, monkeypatch: Any) -> OidcWorld:
    from boss_engine.config import get_settings

    monkeypatch.setenv("BOSS_MASTER_KEY", base64.b64encode(b"o" * 32).decode())
    monkeypatch.delenv("BOSS_MASTER_KEY_FILE", raising=False)
    vault.reset_master_key_cache()
    oidc_keys.clear_cache()
    monkeypatch.setattr(get_settings(), "public_url", ORIGIN)
    ids = IdentityWorld.create(engine)
    with begin() as db:
        db.execute(text("DELETE FROM identity_rate_limits"))     # every test starts with no strikes
        pool = oidc_pools.create(db, org_id=ids.org, actor="usr_test", body={"name": "Staff", "kind": "workforce"})
    http = browser_client()
    return OidcWorld(ids, http, f"{ORIGIN}/oidc/{ids.org}", pool)


_TOKEN_DATA = re.compile(r'data-csrf="([^"]+)"')
_TOKEN_FIELD = re.compile(r'name="csrf" value="([^"]+)"')


def browser_client() -> TestClient:
    """A TestClient that, like a browser on a hosted page, remembers the page's form token."""
    http = TestClient(main.app, raise_server_exceptions=True, base_url=ORIGIN)

    def hook(response: Any) -> None:
        if "text/html" not in response.headers.get("content-type", ""):
            return
        response.read()
        page = response.text
        found = _TOKEN_DATA.search(page)  # the passkey button's, else the step form's (never consent's)
        if found is None and 'name="step"' in page and 'name="step" value="account"' not in page:
            found = _TOKEN_FIELD.search(page)
        if found is not None:
            http.form_token = found.group(1)  # type: ignore[attr-defined]

    http.form_token = ""  # type: ignore[attr-defined]
    http.event_hooks["response"].append(hook)
    return http


def expire_codes() -> None:
    with begin() as db:
        db.execute(text("UPDATE oidc_auth_codes SET expires_at = now() - interval '1 second'"))


# ---- customer pools ---------------------------------------------------------------------------------

STRONG_PW = "violet-otter-harbour-1987"
HOSTED = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin", "Content-Type": "application/x-www-form-urlencoded"}


class SoftAuthenticator:
    """One discoverable ES256 passkey, as a platform authenticator holds it (RP = the BOSS host)."""

    def __init__(self, user_handle: bytes = b"", *, rp_id: str = "boss.oidc.test"):
        import os

        from cryptography.hazmat.primitives.asymmetric import ec

        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.user_handle = user_handle
        self.rp_id = rp_id
        self.sign_count = 0

    def _cose(self) -> bytes:
        import cbor2

        n = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})

    def register(self, options: dict, *, origin: str = ORIGIN) -> dict:
        import json
        import struct

        import cbor2

        self.user_handle = base64.urlsafe_b64decode(options["user"]["id"] + "==")
        cd = json.dumps({"type": "webauthn.create", "challenge": options["challenge"], "origin": origin}).encode()
        ad = (hashlib.sha256(self.rp_id.encode()).digest() + bytes([0x45]) + struct.pack(">I", 0) + b"\0" * 16
              + struct.pack(">H", len(self.credential_id)) + self.credential_id + self._cose())
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": ad})
        return {"id": b64u(self.credential_id), "rawId": b64u(self.credential_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(cd), "attestationObject": b64u(att), "transports": ["internal"]},
                "clientExtensionResults": {}}

    def assert_(self, options: dict, *, origin: str = ORIGIN, user_handle: Optional[bytes] = None) -> dict:
        import json
        import struct

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec

        self.sign_count += 1
        cd = json.dumps({"type": "webauthn.get", "challenge": options["challenge"], "origin": origin}).encode()
        ad = hashlib.sha256(self.rp_id.encode()).digest() + bytes([0x05]) + struct.pack(">I", self.sign_count)
        sig = self.key.sign(ad + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64u(self.credential_id), "rawId": b64u(self.credential_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(cd), "authenticatorData": b64u(ad), "signature": b64u(sig),
                             "userHandle": b64u(user_handle if user_handle is not None else self.user_handle)},
                "clientExtensionResults": {}}


def field_of(html: str, name: str) -> Optional[str]:
    import re

    m = re.search(rf'name="{name}" value="([^"]*)"', html)
    return m.group(1) if m else None


def totp_from_page(html: str) -> str:
    import re

    secret = re.search(r"<code>([A-Z2-7]+)</code>", html).group(1)
    return secret


def totp_code(secret: str, offset: int = 0) -> str:
    from boss_engine.identity import totp

    return totp.code_at(secret, totp.current_step() + offset)


def customer_world(engine: Any, monkeypatch: Any, **pool_over: Any) -> tuple[OidcWorld, list[tuple[str, str, str]]]:
    from boss_engine.identity import email_signin

    w = make_world(engine, monkeypatch)
    mail: list[tuple[str, str, str]] = []
    monkeypatch.setattr(email_signin, "DELIVER", lambda to, subject, body: mail.append((to, subject, body)))
    body = {"name": "Bank customers", "kind": "customer", "signup": "open", "mfa": "required", "passkeys": True}
    body.update(pool_over)
    with begin() as db:
        w.cpool = oidc_pools.create(db, org_id=w.org, actor="usr_test", body=body)  # type: ignore[attr-defined]
    return w, mail


def hosted(w: OidcWorld, step: str, form: dict[str, str], **headers: str):
    h = {**HOSTED, **headers}
    if "csrf" not in form:
        form = {**form, "csrf": getattr(w.http, "form_token", "")}
    return w.http.post(f"/oidc/{w.org}/hosted/{step}", content=urlencode(form), headers=h, follow_redirects=False)


def hosted_json(w: OidcWorld, step: str, body: dict[str, Any]):
    if "csrf" not in body:
        body = {**body, "csrf": getattr(w.http, "form_token", "")}
    return w.http.post(f"/oidc/{w.org}/hosted/{step}", json=body,
                       headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}, follow_redirects=False)


def last_code(mail: list[tuple[str, str, str]]) -> str:
    import re

    return re.search(r"code is (\d{6})", mail[-1][2]).group(1)
