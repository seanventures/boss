"""Test-only pieces for the runner: an ISSUER standing in for the API's signing key (the real
run-token format, signed with a throwaway Ed25519 key), and the API-side DPoP check the real
route makes (identity/dpop.verify_proof, against an in-memory replay cache)."""
from __future__ import annotations

import secrets
import time
from typing import Any, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from boss_engine.identity import dpop, jwks
from boss_engine.identity.delegation import AUD_API, TYP_RUN


class Issuer:
    """Mints `boss-run+jwt` tokens the way identity/tokens._sign_run does (alg, typ, kid)."""

    def __init__(self) -> None:
        self.kid = "kid-test-828"
        self._key = Ed25519PrivateKey.generate()

    def key_for_kid(self, kid: str) -> Any:
        return self._key.public_key() if kid == self.kid else None

    def mint(self, *, run_id: str, jkt: str, ttl: int = 300, aud: str = AUD_API, iat: Optional[int] = None) -> str:
        now = int(time.time()) if iat is None else iat
        claims = {"iss": "urn:boss:local", "typ": TYP_RUN, "aud": aud, "sub": "usr_test", "boss_org": "org_boss",
                  "boss_mode": "obo", "boss_chain": ["agt_builder#" + run_id], "boss_depth": 1, "boss_grant": "grt_1",
                  "boss_task": {"run": run_id, "project": "prj_1", "step": None, "resources": {}},
                  "scope": "code:write", "cnf": {"jkt": jkt}, "jti": "jti_" + secrets.token_hex(8),
                  "iat": now, "exp": now + ttl}
        return jwks.sign({"alg": jwks.ALG, "typ": TYP_RUN, "kid": self.kid}, claims, self._key)


class ReplayDb:
    """What dpop._remember needs from `db`: execute(text(...), params).scalar() with the
    INSERT ... ON CONFLICT DO NOTHING RETURNING 1 semantics of identity_dpop_replay."""

    def __init__(self) -> None:
        self.seen: set[tuple[str, str]] = set()

    def execute(self, statement: Any, params: Optional[dict] = None):
        sql = str(statement)
        outer = self

        class _Result:
            def __init__(self, value):
                self.value = value

            def scalar(self):
                return self.value

        if sql.startswith("INSERT INTO identity_dpop_replay"):
            key = (params["k"], params["j"])
            if key in outer.seen:
                return _Result(None)
            outer.seen.add(key)
            return _Result(1)
        return _Result(None)


def verify_runner_call(headers: dict[str, str], *, method: str, url: str, db: ReplayDb) -> str:
    """The API's check of a runner call: `Authorization: DPoP <token>` + a proof for exactly this
    method and URL, by the key the token is bound to. Returns the token."""
    headers = {k.lower(): v for k, v in headers.items()}
    scheme, _, token = headers.get("authorization", "").partition(" ")
    assert scheme == "DPoP" and token
    claims = jwks.split(token).claims
    dpop.verify_proof(headers.get("dpop"), htm=method, htu=url, access_token=token,
                      expected_jkt=claims["cnf"]["jkt"], db=db)
    return token
