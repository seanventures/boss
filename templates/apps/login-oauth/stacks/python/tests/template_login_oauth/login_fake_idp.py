"""An in-process fake identity provider for the login-oauth tests: no network, no real Google,
GitHub or BOSS. One server plays all three shapes:

    OpenID Connect (Google, BOSS): discovery, /authorize, /token (ID token RS256), /jwks, /userinfo
    GitHub OAuth 2.0:              /login/oauth/authorize, /login/oauth/access_token,
                                   /api/user, /api/user/emails

It enforces what a real provider enforces and the app must get right: a registered client with
the right secret, the exact redirect_uri, one-time codes, and PKCE S256 (the token request's
code_verifier must hash to the authorize request's code_challenge)."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _int(n: int) -> str:
    return b64url(n.to_bytes((n.bit_length() + 7) // 8, "big"))


class FakeIdp:
    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = str(uuid.uuid4())
        nums = self.key.public_key().public_numbers()
        self.jwk = {"kty": "RSA", "n": _int(nums.n), "e": _int(nums.e), "kid": self.kid, "alg": "RS256", "use": "sig"}
        self.clients: dict[str, str] = {}
        self.user = {"sub": "g-1", "email": "ada@example.com", "email_verified": True, "name": "Ada", "picture": None}
        self.tamper: dict = {}
        self.last_authorize: Optional[dict] = None
        self.last_token: Optional[dict] = None
        self.codes: dict[str, dict] = {}
        self.tokens: dict[str, dict] = {}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.issuer = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def sign(self, payload: dict) -> str:
        header = b64url(json.dumps({"alg": "RS256", "typ": "JWT", "kid": self.kid}).encode())
        body = b64url(json.dumps(payload).encode())
        sig = self.key.sign(f"{header}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{header}.{body}.{b64url(sig)}"

    def _handler(self):
        idp = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status: int, body) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _who(self) -> Optional[dict]:
                auth = self.headers.get("Authorization", "")
                return idp.tokens.get(auth[7:]) if auth.lower().startswith("bearer ") else None

            def do_GET(self):  # noqa: N802
                url = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                if url.path == "/.well-known/openid-configuration":
                    i = idp.issuer
                    return self._json(200, {
                        "issuer": i, "authorization_endpoint": f"{i}/authorize", "token_endpoint": f"{i}/token",
                        "userinfo_endpoint": f"{i}/userinfo", "jwks_uri": f"{i}/jwks",
                        "response_types_supported": ["code"], "subject_types_supported": ["public"],
                        "id_token_signing_alg_values_supported": ["RS256"],
                        "code_challenge_methods_supported": ["S256"],
                        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"]})
                if url.path == "/jwks":
                    return self._json(200, {"keys": [idp.jwk]})
                if url.path in ("/authorize", "/login/oauth/authorize"):
                    idp.last_authorize = q
                    if q.get("client_id") not in idp.clients or not q.get("redirect_uri") or q.get("response_type") != "code":
                        return self._json(400, {"error": "invalid_request"})
                    if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
                        return self._json(400, {"error": "invalid_request", "error_description": "PKCE S256 required"})
                    code = secrets.token_urlsafe(24)
                    idp.codes[code] = {**q, "user": dict(idp.user)}
                    back = urlsplit(q["redirect_uri"])
                    extra = {"code": code, **({"state": q["state"]} if "state" in q else {})}
                    query = (back.query + "&" if back.query else "") + urlencode(extra)
                    self.send_response(302)
                    self.send_header("Location", urlunsplit(back._replace(query=query)))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                who = self._who()
                if url.path == "/userinfo":
                    return self._json(200, who) if who else self._json(401, {})
                if url.path == "/api/user":
                    if not who:
                        return self._json(401, {})
                    digits = "".join(ch for ch in who["sub"] if ch.isdigit()) or "1"
                    return self._json(200, {"id": int(digits), "login": who["name"], "name": who["name"],
                                            "avatar_url": who.get("picture")})
                if url.path == "/api/user/emails":
                    if not who:
                        return self._json(401, {})
                    return self._json(200, [{"email": who["email"], "primary": True, "verified": who["email_verified"]}])
                return self._json(404, {"error": "not_found"})

            def do_POST(self):  # noqa: N802
                url = urlsplit(self.path)
                if url.path not in ("/token", "/login/oauth/access_token"):
                    return self._json(404, {"error": "not_found"})
                length = int(self.headers.get("Content-Length") or 0)
                p = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                idp.last_token = p
                client_id, secret = p.get("client_id"), p.get("client_secret")
                auth = self.headers.get("Authorization", "")
                if auth.startswith("Basic "):
                    from urllib.parse import unquote
                    raw = base64.b64decode(auth[6:]).decode()
                    client_id, _, secret = raw.partition(":")
                    client_id, secret = unquote(client_id), unquote(secret)
                if client_id not in idp.clients or idp.clients[client_id] != secret:
                    return self._json(401, {"error": "invalid_client"})
                grant = idp.codes.pop(p.get("code", ""), None)
                if p.get("grant_type") != "authorization_code" or not grant \
                        or grant["client_id"] != client_id or grant["redirect_uri"] != p.get("redirect_uri"):
                    return self._json(400, {"error": "invalid_grant"})
                challenge = b64url(hashlib.sha256((p.get("code_verifier") or "").encode()).digest())
                if challenge != grant["code_challenge"]:
                    return self._json(400, {"error": "invalid_grant", "error_description": "PKCE verification failed"})
                access = secrets.token_urlsafe(24)
                idp.tokens[access] = grant["user"]
                out = {"access_token": access, "token_type": "Bearer", "expires_in": 3600, "scope": grant.get("scope", "")}
                if "openid" in grant.get("scope", "").split():
                    now = int(time.time())
                    u = grant["user"]
                    out["id_token"] = idp.sign({
                        "iss": idp.issuer, "aud": client_id, "sub": u["sub"], "iat": now, "exp": now + 300,
                        "nonce": idp.tamper.get("nonce", grant.get("nonce")), "email": u["email"],
                        "email_verified": u["email_verified"], "name": u["name"], "picture": u.get("picture")})
                return self._json(200, out)

        return Handler
