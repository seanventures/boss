"""The SSRF-safe outbound client for every identity-driven fetch (IDENTITY.md §4.13). Owned by S1;
used by S1 (OIDC discovery, JWKS, token, SAML metadata, social presets) and S3 (GitHub).
FROZEN IN PHASE 0: the signature and the rules.

Resolve DNS once and connect to the pinned IP; refuse loopback, RFC 1918, link-local, CGNAT,
169.254.169.254, ULA and ::1 unless the host is in BOSS_OUTBOUND_ALLOW_PRIVATE; https only (except
allowed private hosts); no cross-host redirects; 5 s connect, 10 s total, 1 MiB body cap.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


class OutboundRefused(Exception):
    """The destination is not allowed (private range, scheme, redirect, size). Never retried."""


@dataclass(frozen=True)
class SafeResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes = field(repr=False)

    def json(self) -> Any:
        import json

        return json.loads(self.body)


def fetch(method: str, url: str, *, headers: Optional[Mapping[str, str]] = None,
          data: Optional[bytes] = None, purpose: str) -> SafeResponse:
    """`purpose` names the caller for the audit of private-host exceptions (e.g. "oidc.jwks")."""
    raise NotImplementedError("identity.http.fetch is stream S1's")
