"""A FAKE OKTA (BOSSAPP-1048): Okta-shaped OIDC discovery, JWKS, token, userinfo and logout, and
Okta's real SCIM request shapes, so the Okta end-to-end tests run on what Okta actually sends.

  issuers   the org authorization server (https://{org}.okta.com: endpoints under /oauth2/v1/)
            and a custom one (https://{org}.okta.com/oauth2/default: endpoints under /v1/)
  keys      RSA, `kid` named like Okta's; `rotate()` publishes a new signing key (and, by
            default, keeps the old one listed for a while, as Okta does)
  tokens    id_tokens with `groups`, `amr`, `sid`, `auth_time`; JWT access tokens; logout tokens
            (OIDC Back-Channel Logout)
  SCIM      the bodies Okta's SCIM 2.0 connector sends (Okta's SCIM test suite reads the same)

Served by replacing sso_oidc._fetch (unknown URLs fall through to the real SSRF-safe client, whose
sockets are forbidden in these tests). Never import this from product code.
"""
from __future__ import annotations

import json
import secrets
import time
import uuid
from typing import Any, Optional
from urllib.parse import parse_qs

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from boss_engine.identity.http import SafeResponse

CLIENT_ID = "0oa1bossconsole5d7"
USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
BACKCHANNEL_EVENT = "http://schemas.openid.net/event/backchannel-logout"


class FakeOkta:
    def __init__(self, org: str = "acme", *, authorization_server: Optional[str] = None,
                 issuer_trailing_slash: bool = False):
        self.host = f"https://{org}.okta.com"
        self.custom = authorization_server
        self.issuer = f"{self.host}/oauth2/{authorization_server}" if authorization_server else self.host
        #: Okta never adds one; this simulates an IdP (or an admin's paste) that does
        self.discovery_issuer = self.issuer + ("/" if issuer_trailing_slash else "")
        prefix = f"{self.issuer}/v1" if authorization_server else f"{self.host}/oauth2/v1"
        self.endpoints = {"authorization_endpoint": f"{prefix}/authorize", "token_endpoint": f"{prefix}/token",
                          "jwks_uri": f"{prefix}/keys", "userinfo_endpoint": f"{prefix}/userinfo",
                          "end_session_endpoint": f"{prefix}/logout"}
        self.keys: list[tuple[str, Any]] = []
        self.rotate(keep_old=False)
        self.client_id = CLIENT_ID
        self.claims: dict[str, Any] = {}
        self.userinfo: dict[str, Any] = {}
        self.nonce: Optional[str] = None
        self.token_requests: list[dict[str, list[str]]] = []
        self.userinfo_calls = 0
        self.issued_id_tokens: list[str] = []
        self.sid = "102" + secrets.token_hex(8)

    # ---- keys ---------------------------------------------------------------------------------
    @property
    def kid(self) -> str:
        return self.keys[-1][0]

    def rotate(self, *, keep_old: bool = True) -> str:
        kid = "okta-" + secrets.token_hex(6)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.keys = ([*self.keys[-1:]] if keep_old and self.keys else []) + [(kid, key)]
        return kid

    def jwks(self) -> dict[str, Any]:
        out = []
        for kid, key in self.keys:
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
            out.append({**jwk, "kid": kid, "use": "sig", "alg": "RS256", "kty": "RSA"})
        return {"keys": out}

    def _sign(self, claims: dict[str, Any], *, kid: Optional[str] = None, key: Any = None) -> str:
        kid = kid or self.kid
        key = key or dict(self.keys)[kid]
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": kid})

    # ---- tokens -------------------------------------------------------------------------------
    def discovery(self) -> dict[str, Any]:
        return {"issuer": self.discovery_issuer, **self.endpoints,
                "response_types_supported": ["code"], "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": ["RS256"],
                "scopes_supported": ["openid", "email", "profile", "groups", "offline_access"],
                "claims_supported": ["iss", "sub", "aud", "email", "email_verified", "name", "groups", "sid", "auth_time"],
                "backchannel_logout_supported": True, "backchannel_logout_session_supported": True}

    def id_token(self) -> str:
        now = int(time.time())
        claims = {"ver": 1, "iss": self.issuer, "aud": self.client_id, "sub": "00u1okta0pat", "iat": now,
                  "exp": now + 3600, "jti": "ID." + secrets.token_urlsafe(12), "amr": ["pwd"],
                  "idp": "00o1okta", "nonce": self.nonce, "auth_time": now - 5, "at_hash": "x",
                  "email": "pat@acme.dev", "email_verified": True, "name": "Pat Okta",
                  "preferred_username": "pat@acme.dev", "groups": ["Everyone", "BOSS Engineers"],
                  "sid": self.sid, **self.claims}
        token = self._sign({k: v for k, v in claims.items() if v is not None})
        self.issued_id_tokens.append(token)
        return token

    def access_token(self) -> str:
        now = int(time.time())
        return self._sign({"ver": 1, "iss": self.issuer, "aud": "api://default", "sub": "pat@acme.dev",
                           "cid": self.client_id, "uid": "00u1okta0pat", "iat": now, "exp": now + 3600,
                           "scp": ["openid", "email", "profile", "groups"]})

    def logout_token(self, *, sub: Optional[str] = "00u1okta0pat", sid: Optional[str] = None,
                     iss: Optional[str] = None, aud: Optional[str] = None, jti: Optional[str] = None,
                     iat: Optional[int] = None, nonce: Optional[str] = None, events: Any = "default",
                     key: Any = None) -> str:
        claims: dict[str, Any] = {"iss": iss or self.issuer, "aud": aud or self.client_id,
                                  "iat": iat or int(time.time()), "jti": jti or uuid.uuid4().hex,
                                  "events": {BACKCHANNEL_EVENT: {}} if events == "default" else events}
        if sub:
            claims["sub"] = sub
        if sid:
            claims["sid"] = sid
        if nonce:
            claims["nonce"] = nonce
        if key is not None:
            return self._sign(claims, kid=self.kid, key=key)
        return self._sign(claims)

    # ---- the door ----------------------------------------------------------------------------
    def fetch(self, real):
        def fetch(method: str, url: str, **kw: Any) -> SafeResponse:
            def j(obj: Any, status: int = 200) -> SafeResponse:
                return SafeResponse(status, {"content-type": "application/json"}, json.dumps(obj).encode())

            if url == f"{self.issuer}/.well-known/openid-configuration":
                return j(self.discovery())
            if url == self.endpoints["jwks_uri"]:
                return j(self.jwks())
            if url == self.endpoints["token_endpoint"]:
                self.token_requests.append(parse_qs((kw.get("data") or b"").decode()))
                return j({"token_type": "Bearer", "expires_in": 3600, "scope": "openid email profile groups",
                          "access_token": self.access_token(), "id_token": self.id_token()})
            if url == self.endpoints["userinfo_endpoint"]:
                self.userinfo_calls += 1
                auth = (kw.get("headers") or {}).get("Authorization", "")
                if not auth.startswith("Bearer "):
                    return j({"error": "invalid_token"}, 401)
                return j({"sub": "00u1okta0pat", "email": "pat@acme.dev", "email_verified": True, **self.userinfo})
            return real(method, url, **kw)
        return fetch


# ---- Okta's SCIM 2.0 request shapes (the Okta SCIM connector and its test suite) -----------------

def scim_create_user(email: str, *, given: str = "Pat", family: str = "Okta", external_id: str = "00u1okta0pat",
                     password: Optional[str] = "Pushed-Pa55word!") -> dict[str, Any]:
    body: dict[str, Any] = {
        "schemas": [USER_SCHEMA], "userName": email,
        "name": {"givenName": given, "familyName": family},
        "emails": [{"primary": True, "value": email, "type": "work"}],
        "displayName": f"{given} {family}", "locale": "en-US", "externalId": external_id,
        "groups": [], "active": True}
    if password is not None:
        body["password"] = password                   # Okta's "Sync password" pushes one
    return body


def scim_filter_username(email: str) -> str:
    return f'userName eq "{email}"'


def scim_deactivate() -> dict[str, Any]:
    return {"schemas": [PATCH_SCHEMA], "Operations": [{"op": "replace", "value": {"active": False}}]}


def scim_reactivate() -> dict[str, Any]:
    return {"schemas": [PATCH_SCHEMA], "Operations": [{"op": "replace", "value": {"active": True}}]}


def scim_patch_name(given: str) -> dict[str, Any]:
    return {"schemas": [PATCH_SCHEMA], "Operations": [{"op": "replace", "path": "name.givenName", "value": given}]}


def scim_patch_add_without_path(family: str, external_id: str) -> dict[str, Any]:
    return {"schemas": [PATCH_SCHEMA], "Operations": [
        {"op": "add", "value": {"name.familyName": family, "externalId": external_id}}]}


def scim_patch_password(password: str = "Rotated-Pa55word!") -> dict[str, Any]:
    return {"schemas": [PATCH_SCHEMA], "Operations": [{"op": "replace", "value": {"password": password}}]}


def scim_put_user(user_id: str, email: str, *, given: str, family: str, external_id: str,
                  active: bool = True) -> dict[str, Any]:
    body = scim_create_user(email, given=given, family=family, external_id=external_id)
    body.update({"id": user_id, "active": active})
    return body


def scim_push_group(name: str, member_ids: list[str]) -> dict[str, Any]:
    return {"schemas": [GROUP_SCHEMA], "displayName": name, "members": [{"value": m, "display": m} for m in member_ids]}


def scim_group_add_members(member_ids: list[str]) -> dict[str, Any]:
    return {"schemas": [PATCH_SCHEMA], "Operations": [
        {"op": "add", "path": "members", "value": [{"value": m, "display": m} for m in member_ids]}]}


def scim_group_remove_member(member_id: str) -> dict[str, Any]:
    return {"schemas": [PATCH_SCHEMA], "Operations": [{"op": "remove", "path": f'members[value eq "{member_id}"]'}]}


def scim_group_rename(group_id: str, name: str) -> dict[str, Any]:
    return {"schemas": [PATCH_SCHEMA], "Operations": [{"op": "replace", "value": {"id": group_id, "displayName": name}}]}
