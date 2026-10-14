"""Postgres-backed rate limits (identity_rate_limits; IDENTITY.md §4.10). Owned by S1; used by S1
(sign-in, MFA, reset, email methods, sign-up, claim) and S3 (token exchange 60/min per run).
FROZEN IN PHASE 0: the signature and the bucket names.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

BUCKETS = frozenset({
    "signin.ip", "signin.account", "mfa.user", "reset.account", "reset.ip", "email_signin.account",
    "email_signin.ip", "signup.account", "signup.ip", "api_key_failure.ip", "token_exchange.run",
    "setup_claim.ip", "setup_link.key",
    # F3/L2: the password soft lock counts failures per (account, device) and (account, IP), so
    # an attacker's failures lock only the attacker's own device and address, never the victim's.
    "signin.account_device", "signin.account_ip",
    # BOSS as an OIDC provider (§6.6 S6.10; amendment AP-4): per IP and per client on authorize and
    # token, and client-secret failures (N in the window lock the client's secret auth).
    "oidc.authorize.ip", "oidc.token.ip", "oidc.token.client", "oidc.client_auth_failure.client",
    # Customer pools (amendment AP-5): the console's sign-in limits, in the pool user's own realm.
    "oidc.signin.account", "oidc.signin.ip", "oidc.signup.ip", "oidc.mfa.user", "oidc.email_code.account",
})


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    retry_after_seconds: Optional[int] = None


def hit(bucket: str, identifier: str, *, limit: int, window_seconds: int, db: Any) -> RateDecision:
    """Count one event. `identifier` is HMAC'd with the server key before it is stored, so a typed
    email (or a password typed into the email box) never lands in the table."""
    if bucket not in BUCKETS:
        raise ValueError(f"unknown rate-limit bucket {bucket!r}")
    if limit < 1 or window_seconds < 1:
        raise ValueError("limit and window_seconds must be positive")
    from sqlalchemy import text

    from .authn_support import keyed_hash

    key_hash = keyed_hash(f"ratelimit:{bucket}", (identifier or "").strip().lower())
    now = time.time()
    start = int(now // window_seconds) * window_seconds
    window_start = datetime.fromtimestamp(start, tz=timezone.utc)
    row = db.execute(text(
        "INSERT INTO identity_rate_limits (bucket, key_hash, window_start, count) "
        "VALUES (:b, :k, :w, 1) ON CONFLICT (bucket, key_hash, window_start) "
        "DO UPDATE SET count = identity_rate_limits.count + 1 RETURNING count"),
        {"b": bucket, "k": key_hash, "w": window_start}).first()
    count = int(row[0]) if row else 1
    if count <= limit:
        return RateDecision(True)
    return RateDecision(False, max(1, int(math.ceil(start + window_seconds - now))))


def peek(bucket: str, identifier: str, *, window_seconds: int, db: Any) -> int:
    """How many events this window holds, without counting one."""
    from sqlalchemy import text

    from .authn_support import keyed_hash

    key_hash = keyed_hash(f"ratelimit:{bucket}", (identifier or "").strip().lower())
    start = int(time.time() // window_seconds) * window_seconds
    row = db.execute(text(
        "SELECT count FROM identity_rate_limits WHERE bucket = :b AND key_hash = :k "
        "AND window_start = :w"), {"b": bucket, "k": key_hash,
                                   "w": datetime.fromtimestamp(start, tz=timezone.utc)}).first()
    return int(row[0]) if row else 0


def reset(bucket: str, identifier: str, *, db: Any) -> None:
    """Forget every window of one identifier (a successful sign-in clears its failure count)."""
    from sqlalchemy import text

    from .authn_support import keyed_hash

    db.execute(text("DELETE FROM identity_rate_limits WHERE bucket = :b AND key_hash = :k"),
               {"b": bucket, "k": keyed_hash(f"ratelimit:{bucket}", (identifier or "").strip().lower())})


def purge(db: Any, *, older_than_seconds: int = 86400) -> None:
    from sqlalchemy import text

    db.execute(text("DELETE FROM identity_rate_limits WHERE window_start < now() - make_interval(secs => :s)"),
               {"s": older_than_seconds})
