"""The legacy bridges: today's shared credentials and claimed actors, each mapped to a real,
named principal so nothing locks out during migration. Owned by stream S5.

FROZEN IN PHASE 0: the bridge kinds, the reserved principal ids and the signatures. IDENTITY.md §12
and the operator decisions in §16 (OD1, OD2):

  - the shared console password: before the first Owner claims, it is the CLAIM credential; after,
    it maps to `usr_legacy_shared` with the Operator role for 30 days, then stops working (OD1);
  - BOSS_API_KEY: `sys_legacy_console` (Owner-equivalent fixed set) until the same cutoff;
  - BOSS_API_KEYS + BOSS_API_KEY_PRINCIPALS: hashed into identity_api_credentials(kind=legacy_env);
  - BOSS_CONSOLE_AUTH=network: a MIGRATION BRIDGE ONLY (OD2). Before the claim it may do exactly
    what today's console does plus claim the first Owner; after the claim it maps to
    `usr_network_operator` (network_bound, Operator) for at most 30 days, then refuses;
  - a claimed `actor` in a body is ignored for authorization, and logged when it differs.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any, Optional

from .authn import RequestCredentials
from .principal import AuthContext

LEGACY_SHARED_PRINCIPAL = "usr_legacy_shared"
LEGACY_CONSOLE_PRINCIPAL = "sys_legacy_console"
NETWORK_OPERATOR_PRINCIPAL = "usr_network_operator"
LEGACY_CUTOFF_DAYS = 30
LEGACY_BRIDGE_ROLE = "operator"


class LegacyKind(StrEnum):
    """identity_legacy_mappings.legacy_kind."""
    MEMBER_ID = "member_id"
    SHARED_PASSWORD = "shared_password"
    CONSOLE_API_KEY = "console_api_key"
    ENV_API_KEY = "env_api_key"
    NETWORK_MODE = "network_mode"
    CLAIMED_ACTOR = "claimed_actor"
    SCIM_BEARER = "scim_bearer"
    OIDC_BEARER = "oidc_bearer"


def bridge(creds: RequestCredentials, *, db: Any = None) -> Optional[AuthContext]:
    """Map what the legacy transport middleware decided (creds.legacy_member_id /
    legacy_auth_method) to an AuthContext for its mapped principal, or None when no legacy path
    applies. Records use in identity_legacy_mappings and refuses a retired path."""
    raise NotImplementedError("identity.legacy.bridge is stream S5's")


def note_claimed_actor(ctx: AuthContext, claimed: Optional[str], *, db: Any = None) -> None:
    """Record `legacy.claimed_actor_ignored` when a body's `actor` differs from ctx.principal."""
    raise NotImplementedError("identity.legacy.note_claimed_actor is stream S5's")
