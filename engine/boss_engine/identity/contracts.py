"""Wire shapes the console depends on: GET /v1/identity/me and GET /v1/identity/me/permissions.

FROZEN IN PHASE 0. The TypeScript mirror is apps/web/lib/identity/contract.ts; the two are kept in
step by apps/web/tests/identity-contract.test.ts (field names) — change both or neither.

No secret ever appears here. The CSRF token is not a secret from the signed-in browser (it is an
HMAC of the session id, useless without the HttpOnly cookie).
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class MeOrg(_Frozen):
    id: str
    name: str


class MeSignInMethods(_Frozen):
    password: bool
    totp: bool
    passkeys: int                       # how many registered
    recovery_codes_remaining: int
    sso_links: list[str]                # provider names (never subjects)


class MeBanner(_Frozen):
    """Console-wide notices the API owns: network bridge, legacy countdown, owner recovered."""
    kind: Literal["network_bridge", "legacy_password", "legacy_api_key", "owner_recovered",
                  "mfa_enrolment_required"]
    message: str
    until: Optional[datetime] = None


class MeResponse(_Frozen):
    """GET /v1/identity/me — who the console is signed in as."""
    principal_id: str
    kind: Literal["user", "service_account", "agent", "system"]
    display_name: str
    email: Optional[str]
    org: MeOrg
    status: Literal["pending", "active", "disabled", "suspended", "revoked"]
    access_roles: list[str]             # built-in or custom role keys bound at org scope
    sign_in: MeSignInMethods
    mfa_required: bool
    mfa_at: Optional[datetime]
    session_id: Optional[str]           # public ses_ id; never the token
    session_expires_at: Optional[datetime]
    csrf_token: str                     # send as X-Boss-CSRF on cookie-authenticated writes
    epoch: int                          # identity epoch; refetch permissions when it changes
    network_bound: bool = False
    banners: list[MeBanner] = []


class MePermissionsResponse(_Frozen):
    """GET /v1/identity/me/permissions?project_id= — the caller's effective permissions there.

    The console mirrors these for UX only (disable with a reason, hide unreachable areas). The API
    re-checks every call; nothing here authorizes anything."""
    epoch: int
    scope: str                          # "org" or "project:<id>"
    permissions: list[str]              # catalogue keys, sorted
    step_up_fresh_until: Optional[datetime]
    catalogue_fingerprint: str          # permissions_gen.CATALOGUE_FINGERPRINT
