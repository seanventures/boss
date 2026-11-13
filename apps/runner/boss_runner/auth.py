"""The runner's half of BOSS's DPoP run-key pattern (identity/dpop.py, identity/runs.py). No new auth.

RUNNER -> API. The runner holds an Ed25519 DPoP key generated at boot and kept in memory only
(like a sandbox shim's). The API mints, for each run it hands over, a `boss:api` run token whose
`cnf.jkt` is THIS key's thumbprint (identity/runs.runner_token — the bridge pattern: a sibling
token of the same run, same person, grant, task and scope, bound to another key). Every call the
runner makes carries `Authorization: DPoP <token>` and a fresh `DPoP` proof for that method and
URL; the API verifies it with the ordinary run-token path (authn._from_run_token), jti replay and
all. The token is useless to anyone without the runner's private key.

API -> RUNNER. The API presents that same runner-bound token. The runner checks it OFFLINE against
the API's published keys (/.well-known/boss-jwks.json): signature, typ, audience, expiry, that it
is bound to THIS runner's key, and that it names the run the request is about. It cannot carry a
proof (only the runner holds the key it is bound to); replaying it can only repeat a command, and
the journal applies a command id once.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from typing import Any, Callable, Mapping, Optional

from boss_engine.identity import dpop, jwks
from boss_engine.identity.delegation import AUD_API, CLOCK_SKEW_SECONDS, TYP_RUN

JWKS_PATH = "/.well-known/boss-jwks.json"


class RunnerAuthError(Exception):
    def __init__(self, code: str, status: int = 401):
        super().__init__(code)
        self.code = code
        self.status = status


class RunnerKey:
    """The runner's DPoP key. Generated at boot; the private half never leaves memory."""

    def __init__(self, key: Optional[dpop.DPoPKey] = None):
        self._key = key or dpop.DPoPKey.generate()

    @property
    def jkt(self) -> str:
        return self._key.jkt

    @property
    def public_jwk(self) -> dict[str, Any]:
        return self._key.public_jwk

    def headers(self, token: str, method: str, url: str) -> dict[str, str]:
        """`Authorization: DPoP <token>` + a fresh proof for exactly this method and URL."""
        return {"Authorization": f"DPoP {token}", "DPoP": self._key.proof(method, url, access_token=token)}


def unverified_claims(token: str) -> dict[str, Any]:
    """The claims of a token the runner itself holds (for its expiry). Never used to authorize."""
    return dict(jwks.split(token).claims)


class JwksKeys:
    """The API's run-token verification keys, fetched from its JWKS document and cached; an
    unknown kid triggers at most one refetch every 30 s (a key rotation)."""

    def __init__(self, api_url: str, *, fetch: Optional[Callable[[str], Mapping[str, Any]]] = None):
        self.url = api_url.rstrip("/") + JWKS_PATH
        self._fetch = fetch or _http_json
        self._keys: dict[str, Any] = {}
        self._fetched_at = 0.0
        self._lock = threading.Lock()

    def __call__(self, kid: str) -> Any:
        with self._lock:
            if kid not in self._keys and time.monotonic() - self._fetched_at > 30:
                self._fetched_at = time.monotonic()
                try:
                    doc = self._fetch(self.url)
                except Exception:  # noqa: BLE001 — an unreachable API is "unknown key", not a crash
                    doc = {}
                for jwk in doc.get("keys") or []:
                    if isinstance(jwk, Mapping) and jwk.get("kid"):
                        try:
                            self._keys[str(jwk["kid"])] = jwks.public_key_from_jwk(jwk)
                        except Exception:  # noqa: BLE001 — skip a key we cannot read
                            continue
            return self._keys.get(kid)


class TokenVerifier:
    """Checks the token the API presents on a call to the runner."""

    def __init__(self, key_for_kid: Callable[[str], Any], *, runner_jkt: str, issuer: Optional[str] = None):
        self.key_for_kid = key_for_kid
        self.runner_jkt = runner_jkt
        self.issuer = issuer

    def verify(self, authorization: Optional[str], *, run_id: Optional[str]) -> tuple[str, dict[str, Any]]:
        """Return (token, claims) or raise RunnerAuthError. `run_id` None skips the run check (the
        caller then checks the token's run against what it loads)."""
        scheme, _, token = (authorization or "").partition(" ")
        token = token.strip()
        if scheme.lower() != "dpop" or not token:
            raise RunnerAuthError("dpop_token_required")
        try:
            _, claims = jwks.verify(token, typ=TYP_RUN, key_for_kid=self.key_for_kid)
        except jwks.JoseError as exc:
            raise RunnerAuthError(f"token_{exc.code}") from exc
        now = time.time()
        if claims.get("aud") != AUD_API:
            raise RunnerAuthError("bad_aud")
        if self.issuer and claims.get("iss") != self.issuer:
            raise RunnerAuthError("bad_iss")
        try:
            exp = int(claims["exp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RunnerAuthError("claims_shape") from exc
        if exp <= now - CLOCK_SKEW_SECONDS:
            raise RunnerAuthError("expired")
        cnf = claims.get("cnf") or {}
        if not isinstance(cnf, Mapping) or cnf.get("jkt") != self.runner_jkt:
            raise RunnerAuthError("not_bound_to_this_runner", 403)
        task = claims.get("boss_task") or {}
        if run_id is not None and (not isinstance(task, Mapping) or task.get("run") != run_id):
            raise RunnerAuthError("token_for_another_run", 403)
        return token, dict(claims)


def run_of(claims: Mapping[str, Any]) -> Optional[str]:
    task = claims.get("boss_task") or {}
    return str(task.get("run")) if isinstance(task, Mapping) and task.get("run") else None


def _http_json(url: str) -> Mapping[str, Any]:
    with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 — the configured API URL
        return json.loads(resp.read().decode("utf-8"))
