"""The OIDC provider's test world (F6, stream S6): a real org, real people with real console
sessions, a workforce pool and clients, driven through the real app. Never import from product code.
"""
from __future__ import annotations

import base64
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
        headers = {"Cookie": f"__Host-boss_session={who.cookie}"} if who else {}
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
        pool = oidc_pools.create(db, org_id=ids.org, actor="usr_test", body={"name": "Staff", "kind": "workforce"})
    http = TestClient(main.app, raise_server_exceptions=True, base_url=ORIGIN)
    return OidcWorld(ids, http, f"{ORIGIN}/oidc/{ids.org}", pool)


def expire_codes() -> None:
    with begin() as db:
        db.execute(text("UPDATE oidc_auth_codes SET expires_at = now() - interval '1 second'"))
