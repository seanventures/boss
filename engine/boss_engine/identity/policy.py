"""The per-org identity policy (identity_settings.policy). Owned by stream S5 (store, API, posture);
read by every stream. FROZEN IN PHASE 0: the keys and their defaults.

One typed view, so no stream reads raw JSON keys of its own invention. Unknown keys in the stored
JSON are ignored; a missing key takes the default below. `PUT /v1/identity/policy` (S5) validates
against this dataclass and writes `policy.changed` with the diff.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class IdentityPolicy:
    # sign-in and sessions (§4.3, §4.6)
    mfa_required_roles: tuple[str, ...] = ("owner", "admin", "auditor")
    session_idle_minutes: int = 12 * 60
    session_absolute_hours: int = 7 * 24
    step_up_minutes: int = 10
    password_min_length: int = 12
    hibp_check: bool = False
    # email sign-in and self sign-up (§4.14, §4.15; D26, D30, D32) — all need SMTP
    email_magic_link: bool = True       # effective only when SMTP is configured
    email_code: bool = True             # effective only when SMTP is configured
    signup_enabled: bool = False
    signup_domains: tuple[str, ...] = ()
    # the public origin bound at claim when BOSS_PUBLIC_URL is unset (§4.12)
    public_origin: Optional[str] = None
    # API keys (§5.3)
    key_default_days: int = 90
    key_max_days: int = 365
    key_allow_no_expiry_for_service_accounts: bool = False
    key_auto_revoke_unused_days: Optional[int] = None
    # delegation (§7.3, §7.4)
    delegation_default_days: int = 30
    delegation_max_days: int = 90
    delegation_allow_until_revoked: bool = False
    org_max_depth: int = 3
    github_jit_credentials: bool = False
    # D28: None = automatic (on exactly when more than one active human exists)
    github_require_user_repo_access: Optional[bool] = None
    # D29: None = automatic (on exactly while one active human exists)
    single_operator_waiver: Optional[bool] = None
    # legacy bridges (D25, D27): days after the first claim
    legacy_cutoff_days: int = 30
    network_bridge_enabled: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


def get_policy(org_id: str, *, db: Any) -> IdentityPolicy:
    """The org's policy with defaults filled in. Cached per identity epoch."""
    raise NotImplementedError("identity.policy.get_policy is stream S5's")
