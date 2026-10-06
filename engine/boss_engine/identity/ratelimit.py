"""Postgres-backed rate limits (identity_rate_limits; IDENTITY.md §4.10). Owned by S1; used by S1
(sign-in, MFA, reset, email methods, sign-up, claim) and S3 (token exchange 60/min per run).
FROZEN IN PHASE 0: the signature and the bucket names.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

BUCKETS = frozenset({
    "signin.ip", "signin.account", "mfa.user", "reset.account", "reset.ip", "email_signin.account",
    "email_signin.ip", "signup.account", "signup.ip", "api_key_failure.ip", "token_exchange.run",
    "setup_claim.ip",
})


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    retry_after_seconds: Optional[int] = None


def hit(bucket: str, identifier: str, *, limit: int, window_seconds: int, db: Any) -> RateDecision:
    """Count one event. `identifier` is HMAC'd with the server key before it is stored, so a typed
    email (or a password typed into the email box) never lands in the table."""
    raise NotImplementedError("identity.ratelimit.hit is stream S1's")
