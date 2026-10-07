"""A real Keycloak for J7 (OIDC and SAML sign-in, group sync), configured through its admin REST API.

THE HOSTNAME SPLIT. The issuer must be one URL that the browser AND the API container both reach.
Keycloak runs as `fxd-keycloak` on the BOSS stack's compose network and listens on the SAME port
inside and outside (http 58290, https 58291), so `http://fxd-keycloak:58290` resolves through
Docker DNS for the API, and through Chromium's `--host-resolver-rules=MAP fxd-keycloak 127.0.0.1`
for the browser (`browser_args()`). KC_HOSTNAME pins every URL Keycloak issues to that name.
The API's SSRF-safe client (§4.13) refuses private addresses and plain http unless the host is in
BOSS_OUTBOUND_ALLOW_PRIVATE, so the stack J7 runs on sets BOSS_OUTBOUND_ALLOW_PRIVATE=fxd-keycloak.
SAML's SSO URL must be https (BOSS refuses an http one); only the browser goes there, so the https
listener uses a throwaway self-signed certificate and the browser context ignores TLS errors.

The admin password is generated per run, kept in memory only, and never printed.
"""
from __future__ import annotations

import json
import secrets
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional

from e2e.identity import kit as K

NAME = "fxd-keycloak"
IMAGE = "quay.io/keycloak/keycloak:26.0"
HTTP_PORT = 58290
HTTPS_PORT = 58291
ISSUER_BASE = f"http://{NAME}:{HTTP_PORT}"
ADMIN = f"http://127.0.0.1:{HTTP_PORT}"


def browser_args() -> list[str]:
    return [f"--host-resolver-rules=MAP {NAME} 127.0.0.1"]


class Keycloak:
    def __init__(self, network: str):
        self.network = network
        self.admin_user = "fxd-admin"
        self.admin_password = K.secret(secrets.token_urlsafe(24)) or ""
        self._token: Optional[str] = None
        self._token_at = 0.0
        self._certdir: Optional[tempfile.TemporaryDirectory] = None
        self._https: set[str] = set()

    # ---- lifecycle ---------------------------------------------------------------------------------
    def start(self, wait_s: int = 240) -> None:
        self.stop()
        self._certdir = tempfile.TemporaryDirectory(prefix="fxd-kc-")
        d = Path(self._certdir.name)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                        "-subj", f"/CN={NAME}", "-addext", f"subjectAltName=DNS:{NAME},DNS:localhost",
                        "-keyout", str(d / "key.pem"), "-out", str(d / "cert.pem")],
                       check=True, capture_output=True)
        (d / "key.pem").chmod(0o644)
        cmd = ["docker", "run", "-d", "--name", NAME, "--network", self.network,
               "-p", f"127.0.0.1:{HTTP_PORT}:{HTTP_PORT}", "-p", f"127.0.0.1:{HTTPS_PORT}:{HTTPS_PORT}",
               "-v", f"{d}:/certs:ro",
               "-e", f"KC_BOOTSTRAP_ADMIN_USERNAME={self.admin_user}",
               "-e", f"KC_BOOTSTRAP_ADMIN_PASSWORD={self.admin_password}",
               "-e", f"KC_HTTP_PORT={HTTP_PORT}", "-e", f"KC_HTTPS_PORT={HTTPS_PORT}",
               "-e", "KC_HTTP_ENABLED=true",
               "-e", "KC_HTTPS_CERTIFICATE_FILE=/certs/cert.pem", "-e", "KC_HTTPS_CERTIFICATE_KEY_FILE=/certs/key.pem",
               "-e", f"KC_HOSTNAME={ISSUER_BASE}", "-e", "KC_HOSTNAME_STRICT=false",
               "-m", "1g", IMAGE, "start-dev"]
        done = subprocess.run(cmd, capture_output=True, text=True)
        if done.returncode != 0:
            raise RuntimeError("keycloak did not start: " + K.scrub(done.stderr)[-300:])
        deadline = time.time() + wait_s
        while time.time() < deadline:
            st, _ = K.http("GET", f"{ADMIN}/realms/master/.well-known/openid-configuration", timeout=5)
            if st == 200:
                return
            time.sleep(3)
        raise RuntimeError("keycloak did not answer within %ss" % wait_s)

    def stop(self) -> None:
        subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)
        if self._certdir:
            self._certdir.cleanup()
            self._certdir = None

    @staticmethod
    def running() -> bool:
        out = subprocess.run(["docker", "ps", "-a", "--filter", f"name=^{NAME}$", "--format", "{{.Names}}"],
                             capture_output=True, text=True).stdout.strip()
        return bool(out)

    # ---- admin REST ----------------------------------------------------------------------------------
    def token(self) -> str:
        if self._token and time.time() - self._token_at < 40:
            return self._token
        body = urllib.parse.urlencode({"grant_type": "password", "client_id": "admin-cli",
                                       "username": self.admin_user, "password": self.admin_password}).encode()
        st, j = K.http("POST", f"{ADMIN}/realms/master/protocol/openid-connect/token", data=body,
                       headers={"Content-Type": "application/x-www-form-urlencoded"})
        if st != 200:
            raise RuntimeError(f"keycloak admin token: {st}")
        self._token = K.secret(j["access_token"])
        self._token_at = time.time()
        return self._token or ""

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        h = {"Authorization": f"Bearer {self.token()}"}
        if body is not None:
            h["Content-Type"] = "application/json"
        return K.http(method, f"{ADMIN}/admin{path}", data=data, headers=h)

    def ok(self, method: str, path: str, body: Any = None) -> Any:
        st, j = self.call(method, path, body)
        if st >= 300:
            raise RuntimeError(f"keycloak {method} {path}: {st} {K.short(j, 200)}")
        return j

    # ---- realm setup -----------------------------------------------------------------------------------
    def realm(self, name: str, *, https_frontend: bool = False) -> None:
        """A realm. `https_frontend` pins its URLs to the https listener (SAML: BOSS needs an https
        SSO URL, and Keycloak checks the request's Destination against its own frontend URL)."""
        body: dict[str, Any] = {"realm": name, "enabled": True, "sslRequired": "none", "loginWithEmailAllowed": True}
        if https_frontend:
            body["attributes"] = {"frontendUrl": f"https://{NAME}:{HTTPS_PORT}"}
            self._https.add(name)
        self.ok("POST", "/realms", body)

    def issuer(self, realm: str) -> str:
        base = f"https://{NAME}:{HTTPS_PORT}" if realm in self._https else ISSUER_BASE
        return f"{base}/realms/{realm}"

    def group(self, realm: str, name: str) -> str:
        self.ok("POST", f"/realms/{realm}/groups", {"name": name})
        groups = self.ok("GET", f"/realms/{realm}/groups?search={urllib.parse.quote(name)}")
        return next(g["id"] for g in groups if g["name"] == name)

    def user(self, realm: str, username: str, email: str, password: str, first: str = "FXD",
             last: str = "Person", groups: tuple[str, ...] = ()) -> str:
        K.secret(password)
        self.ok("POST", f"/realms/{realm}/users", {
            "username": username, "email": email, "emailVerified": True, "enabled": True,
            "firstName": first, "lastName": last, "requiredActions": [],
            "credentials": [{"type": "password", "value": password, "temporary": False}]})
        users = self.ok("GET", f"/realms/{realm}/users?username={urllib.parse.quote(username)}&exact=true")
        uid = users[0]["id"]
        for gid in groups:
            self.ok("PUT", f"/realms/{realm}/users/{uid}/groups/{gid}")
        return uid

    def leave_group(self, realm: str, uid: str, gid: str) -> None:
        self.ok("DELETE", f"/realms/{realm}/users/{uid}/groups/{gid}")

    def logout_user(self, realm: str, uid: str) -> None:
        self.call("POST", f"/realms/{realm}/users/{uid}/logout")

    def oidc_client(self, realm: str, client_id: str, redirect_base: str) -> str:
        secret = K.secret(secrets.token_urlsafe(24)) or ""
        self.ok("POST", f"/realms/{realm}/clients", {
            "clientId": client_id, "protocol": "openid-connect", "publicClient": False, "secret": secret,
            "standardFlowEnabled": True, "directAccessGrantsEnabled": False,
            "redirectUris": [f"{redirect_base}/*"], "webOrigins": [redirect_base],
            "attributes": {"pkce.code.challenge.method": "S256"},
            "protocolMappers": [{
                "name": "groups", "protocol": "openid-connect", "protocolMapper": "oidc-group-membership-mapper",
                "config": {"claim.name": "groups", "full.path": "false", "id.token.claim": "true",
                           "access.token.claim": "true", "userinfo.token.claim": "true"}}]})
        return secret

    def saml_client(self, realm: str, sp_entity_id: str, acs: str) -> None:
        self.ok("POST", f"/realms/{realm}/clients", {
            "clientId": sp_entity_id, "protocol": "saml", "enabled": True, "frontchannelLogout": False,
            "redirectUris": [acs],
            "attributes": {
                "saml_assertion_consumer_url_post": acs,
                "saml.client.signature": "false", "saml.authnstatement": "true",
                "saml.server.signature": "true", "saml.assertion.signature": "true",
                "saml.signature.algorithm": "RSA_SHA256", "saml_signature_canonicalization_method":
                    "http://www.w3.org/2001/10/xml-exc-c14n#",
                "saml_name_id_format": "persistent", "saml.force.name.id.format": "false",
                "saml.encrypt": "false"},
            "protocolMappers": [
                {"name": "email", "protocol": "saml", "protocolMapper": "saml-user-property-mapper",
                 "config": {"user.attribute": "email", "attribute.name": "email",
                            "attribute.nameformat": "Basic", "friendly.name": "email"}},
                {"name": "groups", "protocol": "saml", "protocolMapper": "saml-group-membership-mapper",
                 "config": {"attribute.name": "groups", "attribute.nameformat": "Basic",
                            "full.path": "false", "single": "false"}}]})

    def signing_cert_pem(self, realm: str) -> str:
        keys = self.ok("GET", f"/realms/{realm}/keys")
        for k in keys.get("keys", []):
            if k.get("algorithm") == "RS256" and k.get("certificate") and k.get("use", "SIG") == "SIG":
                body = k["certificate"]
                lines = [body[i:i + 64] for i in range(0, len(body), 64)]
                return "-----BEGIN CERTIFICATE-----\n" + "\n".join(lines) + "\n-----END CERTIFICATE-----\n"
        raise RuntimeError("no RS256 signing certificate in realm " + realm)

    def saml_sso_url(self, realm: str) -> str:
        return f"https://{NAME}:{HTTPS_PORT}/realms/{realm}/protocol/saml"


def login_at_keycloak(page: Any, username: str, password: str, timeout_ms: int = 30000) -> None:
    """Keycloak's own login form."""
    page.wait_for_selector("#username", timeout=timeout_ms)
    page.fill("#username", username)
    page.fill("#password", password)
    page.click("#kc-login")
