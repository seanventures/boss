"""R7 (IDENTITY.md §15.4): every catalogue permission is used by a route, a tool or a console page,
or is named below with the reason it has none yet. A key nobody checks is a key nobody notices
being granted.
"""
from __future__ import annotations

import re
from pathlib import Path

from apps.api.app import identity_guard
from apps.api.app.main import app
from boss_engine.identity.declarations import Permit
from boss_engine.identity.permissions_gen import PERMISSION_META
from boss_engine.tools import build_default_registry

ROOT = Path(__file__).resolve().parents[3]

#: Checked by identity routes other streams own (IDENTITY.md §10): S1 people/keys/sessions,
#: S3 connections and delegations, S5 policy.
IDENTITY_API = frozenset({
    "api_key:read", "api_key:revoke", "connection:read_all", "connection:revoke_any",
    "delegation:read_all", "security_policy:read", "session:read", "session:revoke",
    "user:delete", "user:update",
})
#: No route exercises these today; each is reserved for a named future operation.
RESERVED = {
    "org:read": "GET /v1/orgs is authenticated and filters to the caller's org (§6.7)",
    "org:delete": "no org deletion route exists (one org per install, D24)",
    "project:create": "projects are created by setup and seed, not by a route yet",
    "project:delete": "no project deletion route exists",
    "telemetry:ingest": "the collector's machine routes are public:collector_signed (A1); kept "
                        "for an authenticated collector principal",
    "app_secret:live": "checked inside PUT /v1/apps/{app_key}/secrets/{name} when mode=live, on "
                       "top of the route's app_secret:write (APP_TEMPLATES_AND_SECURITY_LOOP.md §2.6)",
    "oidc_app:sign_in": "checked by the OIDC authorize endpoint for a workforce-pool sign-in; the "
                        "route itself is public:oidc_provider_endpoint (§6.3)",
}


def used() -> set[str]:
    keys = {e.access.permission for e in identity_guard.route_inventory(app) if isinstance(e.access, Permit)}
    keys |= {s.permission for s in build_default_registry().specs() if s.permission}
    areas = (ROOT / "apps" / "web" / "lib" / "identity" / "areas.ts").read_text(encoding="utf-8")
    keys |= {k for k in re.findall(r'"([a-z_]+:[a-z_]+)"', areas) if k in PERMISSION_META}
    return keys


def test_every_catalogue_key_is_used_or_explained():
    orphans = set(PERMISSION_META) - used() - IDENTITY_API - set(RESERVED)
    assert not orphans, f"no route, tool or page uses {sorted(orphans)}"


def test_the_explanations_are_not_stale():
    assert not (used() & set(RESERVED)), "a reserved key is used now: remove it from RESERVED"
    assert IDENTITY_API <= set(PERMISSION_META) and set(RESERVED) <= set(PERMISSION_META)
