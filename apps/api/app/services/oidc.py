from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Optional

import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWTError

from ..config import Settings


class OidcConfigurationError(RuntimeError):
    pass


class OidcAuthenticationError(RuntimeError):
    pass


@dataclass
class OidcPrincipal:
    member_id: str
    subject: str
    issuer: str
    claims: dict[str, Any]


@lru_cache(maxsize=16)
def jwks_client_for_url(jwks_url: str) -> PyJWKClient:
    return PyJWKClient(jwks_url)


class OidcAuthenticator:
    def __init__(self, settings: Settings):
        self.settings = settings

    def verify_bearer_token(self, token: str) -> OidcPrincipal:
        if not self.settings.oidc_enabled:
            raise OidcAuthenticationError("OIDC authentication is not enabled.")
        if not self.settings.oidc_configured:
            raise OidcConfigurationError(
                "OIDC authentication is enabled but issuer, audience, JWKS URL, "
                "or allowed algorithms are not configured."
            )
        try:
            signing_key = jwks_client_for_url(
                str(self.settings.oidc_jwks_url)
            ).get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(self.settings.oidc_algorithm_values),
                audience=self.settings.oidc_audience,
                issuer=self.settings.oidc_issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except PyJWTError as error:
            raise OidcAuthenticationError(f"Invalid OIDC bearer token: {error}") from error

        member_id = self._principal_from_claims(claims)
        if not member_id:
            raise OidcAuthenticationError(
                "OIDC token did not resolve to a BOSS project member principal."
            )
        return OidcPrincipal(
            member_id=member_id,
            subject=str(claims.get("sub")),
            issuer=str(claims.get("iss")),
            claims=claims,
        )

    def _principal_from_claims(self, claims: dict[str, Any]) -> Optional[str]:
        claim_name = self.settings.oidc_principal_claim.strip()
        if claim_name:
            claim_value = claims.get(claim_name)
            if isinstance(claim_value, str) and claim_value.strip():
                return claim_value.strip()

        subject = claims.get("sub")
        if isinstance(subject, str):
            mapped = self.settings.oidc_subject_principal_map.get(subject)
            if mapped:
                return mapped
        return None
