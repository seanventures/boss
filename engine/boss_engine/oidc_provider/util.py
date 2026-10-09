"""Shared pieces of the OIDC provider: ids, hashing, lifetimes, the protocol error, audit."""
from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

#: Lifetimes (§6.2).
CODE_TTL = timedelta(seconds=60)
ACCESS_TTL = timedelta(minutes=10)
ID_TOKEN_TTL = timedelta(minutes=10)
REFRESH_TTL = timedelta(days=30)          # one refresh token, if never used
FAMILY_MAX_AGE = timedelta(days=90)       # a refresh family (one grant), however often rotated
INTERACTION_TTL = timedelta(minutes=10)
CLOCK_SKEW = timedelta(seconds=60)

#: Scopes BOSS knows (§6.2). A client may only be granted what it registered.
SUPPORTED_SCOPES = ("openid", "profile", "email", "roles", "offline_access")
ACR_MFA = "urn:boss:acr:mfa"
ACR_PWD = "urn:boss:acr:pwd"

#: Token types (`typ`), never accepted by BOSS's own verifiers (which want boss-run+jwt etc.).
TYP_ID = "JWT"
TYP_ACCESS = "at+jwt"


class OidcError(Exception):
    """A protocol error. `code` is the RFC error code; `description` is safe to show the client
    (never a secret, never which check failed beyond what the RFC code says)."""

    def __init__(self, code: str, description: str = "", status: int = 400):
        super().__init__(code)
        self.code = code
        self.description = description
        self.status = status


def now() -> datetime:
    return datetime.now(timezone.utc)


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def s256(verifier: str) -> str:
    """The PKCE S256 transform (RFC 7636 §4.2)."""
    return b64u(hashlib.sha256(verifier.encode("ascii")).digest())


def random_token(prefix: str, nbytes: int = 32) -> str:
    return f"{prefix}{b64u(secrets.token_bytes(nbytes))}"


def new_id(prefix: str) -> str:
    raw = base64.b32encode(secrets.token_bytes(17)).decode("ascii").rstrip("=").lower()
    return f"{prefix}_{raw[:26]}"


def audit(db: Any, *, org_id: str, event_type: str, outcome: str = "success",
          actor_principal_id: Optional[str] = None, actor_kind: Optional[str] = None,
          target_type: Optional[str] = None, target_id: Optional[str] = None,
          session_ref: Optional[str] = None, ip: Optional[str] = None,
          details: Optional[Mapping[str, Any]] = None) -> None:
    """One security event in the caller's transaction (S6.8). Details never carry a code, token,
    secret or password: audit.record refuses credential-named keys."""
    from ..identity import audit as _audit

    _audit.record(_audit.SecurityEvent(
        org_id=org_id, event_type=event_type, outcome=_audit.Outcome(outcome),
        actor_principal_id=actor_principal_id, actor_kind=actor_kind, target_type=target_type,
        target_id=target_id, session_ref=session_ref, ip=ip, details=dict(details or {})), db=db)
