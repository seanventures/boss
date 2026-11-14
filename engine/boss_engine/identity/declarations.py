"""How an operation declares its access: `@permit`, `@public`, `@authenticated`.

FROZEN IN PHASE 0. IDENTITY.md §6.3. Framework-free: a declaration is metadata attached to the
endpoint function (attribute `__boss_access__`); the API's enforcement point
(apps/api/app/identity_guard.py) reads it on every request, and the boot-time coverage check walks
every route for it.

    @app.post("/v1/workflow-runs/{run_id}/pause")
    @permit(P.WORKFLOW_RUN_CONTROL, scope=Scope.of("workflow_run", path="run_id"))
    def pause_run(run_id: str, ...): ...

    @app.post("/v1/webhooks/github")
    @public(reason=PublicReason.HMAC_SIGNED_WEBHOOK, verifier=verify_github_signature)
    def github_webhook(...): ...

    @app.get("/v1/console/v2/overview")
    @authenticated(composite=True)          # each section filters by its own permission
    def overview(...): ...

Exactly one declaration per endpoint. The decorator validates at IMPORT time: an unknown permission,
a project-scoped permission without a scope resolver, or an org-only permission with a project
resolver raises immediately, so a bad declaration cannot reach a running server.

System-only operations (the worker protocol, collector ingest, SCIM) are ordinary `@permit`s of
system-only permissions (`worker:lease`, `telemetry:ingest`, `scim:provision`): only the fixed system
roles hold those, so no person can call them.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Optional, TypeVar, Union

from .catalogue import CatalogueError, meta, require_known
from .object_rules import Confine, Production
from .scope import Scope, ScopeSource, ScopeSpec

ACCESS_ATTR = "__boss_access__"

F = TypeVar("F", bound=Callable[..., Any])


class PublicReason(StrEnum):
    """The closed list of reasons an operation may skip authentication. Each names the mechanism
    that protects the route instead. Adding one is a spec amendment (IDENTITY_BUILD.md §7)."""
    LIVENESS = "liveness"                                   # /health: says nothing
    AUTH_ENDPOINT = "auth_endpoint"                         # /v1/auth/*: rate-limited, CSRF-checked
    HMAC_SIGNED_WEBHOOK = "hmac_signed_webhook"             # GitHub, Plane, Bitbucket, alerts
    OAUTH_STATE_BOUND_CALLBACK = "oauth_state_bound_callback"  # single-use state nonce
    SAML_SIGNED_ASSERTION = "saml_signed_assertion"         # ACS + SP metadata
    PER_INVESTIGATION_CALLBACK_TOKEN = "per_investigation_callback_token"
    JWKS = "jwks"                                           # public keys only
    PREVIEW_CAPABILITY_URL = "preview_capability_url"       # unguessable preview id + TTL (A1 amendment)
    COLLECTOR_SIGNED = "collector_signed"                   # Improve-BOSS collector machine routes (A1)
    LOOPBACK_HOST_ONLY = "loopback_host_only"               # answers only to a loopback peer (A1)
    API_DESCRIPTION = "api_description"                     # /openapi.json, /docs (A1; S2 decides)
    EGRESS_SERVICE_CREDENTIAL = "egress_service_credential"  # /v1/identity/introspect: sys_egress's key only (A40)
    OIDC_PROVIDER_ENDPOINT = "oidc_provider_endpoint"       # /oidc/<org>/*: BOSS as an OP for its apps (AP-2):
                                                            # client auth, PKCE, the app session or an app token
    MCP_GATEWAY_TOKEN = "mcp_gateway_token"                 # /mcp/gw/{id}: that gateway's own hashed, expiring,
                                                            # revocable token (BOSSAPP-851 amendment)
    IDP_SIGNED_LOGOUT_TOKEN = "idp_signed_logout_token"     # /v1/auth/sso/{id}/backchannel-logout: a logout
                                                            # token signed by that provider's keys; it can
                                                            # only END sessions (BOSSAPP-1048 amendment)


#: Reasons whose route has nothing to verify, so `verifier` may be None.
NO_VERIFIER_NEEDED = frozenset({PublicReason.LIVENESS, PublicReason.JWKS,
                                PublicReason.API_DESCRIPTION})


@dataclass(frozen=True)
class Permit:
    permission: str
    scope: ScopeSpec
    readonly_post: bool = False
    #: A GET that writes (seeds defaults, reaps, freezes, probes). A cookie-authenticated GET to it
    #: is treated like a write for CSRF (the session cookie is SameSite=Lax, so a cross-site
    #: top-level navigation carries it): it needs X-Boss-CSRF or Sec-Fetch-Site: same-origin.
    #: Each such route also answers POST, the method its callers move to (amendment, int-b).
    writes_on_get: bool = False
    #: When set, the GET writes ONLY when one of these query parameters has one of these values
    #: (`check=stale|now` probes; `check=none` is a plain read): CSRF is required only then, so a
    #: page reading the list never needs a token (F2, GET /v1/mcp/servers).
    writes_on_get_if: tuple[tuple[str, tuple[str, ...]], ...] = ()
    #: Ids in the body/query/path that must belong to the scope's project (object_rules.Confine).
    confine: tuple[Confine, ...] = ()
    #: Where the operation's deployment target comes from; a production target also needs
    #: deployment:production at the target's project (object_rules.Production).
    production: Optional[Production] = None

    def describe(self) -> str:
        return f"permit {self.permission} @ {self.scope.describe()}"


@dataclass(frozen=True)
class Public:
    reason: PublicReason
    #: The function that authenticates this request by other means (an HMAC check, a state nonce
    #: lookup). Referenced so reviewers and the coverage test can find it; Phase 0's enforcement
    #: point does not call it (the handler still does). S2 may move the call into the guard.
    verifier: Optional[Callable[..., Any]]

    def describe(self) -> str:
        return f"public:{self.reason.value}"


@dataclass(frozen=True)
class Authenticated:
    """Any ACTIVE principal. For `/me/*` (self_only) and composite console views (composite: the
    handler must filter every section by that section's own permission).

    `people_only`: a PERSON signed in with a session, never a service account, API key, run token
    or bridge (the handler's `need_person_session` answers anyone else 403 people_only). Declared,
    so the catalogue, the matrix and the e2e API sweep know it; test_identity_people_only_declared
    holds the declaration and the handler's gate to each other."""
    composite: bool = False
    self_only: bool = False
    note: str = ""
    people_only: bool = False

    def describe(self) -> str:
        return "authenticated" + (":composite_view" if self.composite else "") + (
            ":self" if self.self_only else "") + (":people" if self.people_only else "")


Access = Union[Permit, Public, Authenticated]


def access_of(endpoint: Any) -> Optional[Access]:
    """The declaration on an endpoint function, or None when it has none."""
    return getattr(endpoint, ACCESS_ATTR, None)


def _attach(fn: F, access: Access) -> F:
    existing = getattr(fn, ACCESS_ATTR, None)
    if existing is not None and existing != access:
        raise CatalogueError(
            f"{getattr(fn, '__qualname__', fn)} already declares {existing.describe()}; "
            "an operation has exactly one declaration")
    setattr(fn, ACCESS_ATTR, access)
    return fn


def permit(permission: str, scope: Optional[ScopeSpec] = None, *,
           readonly_post: bool = False, writes_on_get: bool = False,
           confine: tuple[Confine, ...] | list[Confine] = (),
           production: Optional[Production] = None,
           writes_on_get_if: Optional[dict[str, tuple[str, ...] | list[str]]] = None
           ) -> Callable[[F], F]:
    """Declare the catalogue permission an operation needs, and where to check it.

    `scope` may be omitted only for an org-only permission (it defaults to `Scope.org()`); a
    project-scoped permission must say how to find its project. An org-only permission cannot be
    checked at a project (IDENTITY.md §6.7). `readonly_post` marks a POST that changes nothing
    (a search); it is the declared exception to the read-only-GET test (§15.4 R5).
    `writes_on_get`, `confine` and `production` are the object rules (object_rules.py)."""
    key = require_known(str(permission))
    m = meta(key)
    if scope is None:
        if not m.org_only and not m.system_only:
            raise CatalogueError(f"{key} is project-scoped: declare scope=Scope.project(...), "
                                 "Scope.of(...) or, for an any-project check, Scope.org()")
        scope = Scope.org()
    if m.org_only and scope.source != ScopeSource.ORG:
        raise CatalogueError(f"{key} is org-only and cannot be checked at {scope.describe()}")
    if production is not None and not isinstance(production, Production):
        raise CatalogueError("production= takes an object_rules.Production(...)")
    if any(not isinstance(c, Confine) for c in confine):
        raise CatalogueError("confine= takes object_rules.Confine entries")
    if writes_on_get_if and not writes_on_get:
        raise CatalogueError("writes_on_get_if= qualifies writes_on_get=True")
    declaration = Permit(permission=key, scope=scope, readonly_post=readonly_post,
                         writes_on_get=writes_on_get, confine=tuple(confine),
                         production=production,
                         writes_on_get_if=tuple((k, tuple(v)) for k, v in
                                                sorted((writes_on_get_if or {}).items())))

    def deco(fn: F) -> F:
        return _attach(fn, declaration)

    return deco


def public(*, reason: PublicReason | str,
           verifier: Optional[Callable[..., Any]] = None) -> Callable[[F], F]:
    """Declare an unauthenticated operation and the mechanism that protects it instead."""
    why = PublicReason(reason)
    if verifier is None and why not in NO_VERIFIER_NEEDED:
        raise CatalogueError(f"public:{why.value} needs verifier= (the function that checks it)")
    declaration = Public(reason=why, verifier=verifier)

    def deco(fn: F) -> F:
        return _attach(fn, declaration)

    return deco


def authenticated(*, composite: bool = False, self_only: bool = False,
                  note: str = "", people_only: bool = False) -> Callable[[F], F]:
    """Declare an operation open to any active principal (see `Authenticated`); `people_only`
    narrows it to a person's own session."""
    declaration = Authenticated(composite=composite, self_only=self_only, note=note,
                                people_only=people_only)

    def deco(fn: F) -> F:
        return _attach(fn, declaration)

    return deco
