"""THE API's enforcement point: every route goes through `enforce_access`.

docs/product/IDENTITY.md §6.3/§6.6, frozen in Phase 0 (docs/product/IDENTITY_BUILD.md §2.4).

    app = FastAPI(..., dependencies=[Depends(identity_guard.enforce_access)])

An app-level dependency is copied onto every route the app or any included router registers, so no
route can opt out; it runs after the transport middleware (it sees what that decided) and after
routing (it knows the matched route, its declaration and its path parameters).

What it does per request, by mode (BOSS_AUTHZ_MODE, per-router BOSS_AUTHZ_ROUTER_MODES):
  off      nothing.
  shadow   evaluate the route's declaration (authn -> scope -> authz.check); record every would-deny
           in the shadow ledger (first occurrence logged); never refuse. DEFAULT.
  enforce  refuse a deny with its status and the spec's error body; refuse 503 when the decision
           cannot be made (fail closed).

At boot `check_app_coverage(app)` walks every route and lists the undeclared ones; with
BOSS_AUTHZ_COVERAGE=fail (the default under enforce) the API refuses to start.

Stream ownership: this file is Phase 0's; S1 wires real authentication in identity.authn, S2 the
decision in identity.authz and the declarations on routes, S5 the legacy bridge, the ledger flush
and the per-router rollout. None of them should need to edit this file; an edit is an amendment.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Callable, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import WebSocketException
from starlette.requests import HTTPConnection, Request
from starlette.routing import Mount, Route, WebSocketRoute

from boss_engine.config import get_settings
from boss_engine.identity import authn, authz, legacy
from boss_engine.identity import scope as scope_module
from boss_engine.identity.declarations import Access, Permit, access_of
from boss_engine.identity.enforcement import (
    LEDGER,
    AuthzMode,
    CoverageReport,
    Outcome,
    RouteEntry,
    check_coverage,
    coverage_policy,
    evaluate,
    mode_for,
)
from boss_engine.identity.scope import RequestValues

#: Set by main.py once its transport contextvars exist (the legacy bridge reads them).
_legacy_member: Callable[[], Optional[str]] = lambda: None  # noqa: E731
_legacy_method: Callable[[], Optional[str]] = lambda: None  # noqa: E731

#: Framework routes (Starlette's /openapi.json, /docs, /redoc) have no endpoint function to carry a
#: decorator; S2 declares them here by path. The API description is public: BOSS is open source,
#: the document names routes and shapes, never data (PublicReason.API_DESCRIPTION, amendment A1).
from boss_engine.identity.declarations import Public, PublicReason  # noqa: E402

FRAMEWORK_DECLARATIONS: dict[str, Access] = {
    path: Public(reason=PublicReason.API_DESCRIPTION, verifier=None)
    for path in ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc")
}


class AccessRefused(Exception):
    """Raised by the dependency under enforce; rendered by the handler `install` registers."""

    def __init__(self, outcome: Outcome):
        super().__init__(outcome.reason)
        self.outcome = outcome


def configure(*, legacy_member: Callable[[], Optional[str]],
              legacy_method: Callable[[], Optional[str]]) -> None:
    global _legacy_member, _legacy_method
    _legacy_member, _legacy_method = legacy_member, legacy_method


def router_key_for(endpoint: Any) -> str:
    """The rollout unit: the endpoint's module tail (`main`, `setup`, `identity_auth`, ...)."""
    module = getattr(endpoint, "__module__", "") or ""
    return module.rsplit(".", 1)[-1] or "unknown"


def _credentials(conn: HTTPConnection) -> authn.RequestCredentials:
    client = conn.client.host if conn.client else None
    return authn.RequestCredentials(
        method=conn.scope.get("method", "WEBSOCKET"),
        path=conn.scope.get("path", ""),
        headers={k.lower(): v for k, v in conn.headers.items()},
        cookies=dict(conn.cookies),
        client_ip=client,
        peer_ip=client,
        request_id=conn.headers.get("x-request-id"),
        legacy_member_id=_legacy_member(),
        legacy_auth_method=_legacy_method(),
    )


#: Set for the rest of a request once the guard has ALLOWED it under `enforce`: the decision was
#: made here, against the catalogue, so main.py's legacy actor/member checks (zone L) defer to it.
#: Never set in shadow mode, where the legacy checks still decide.
IDENTITY_ENFORCED: ContextVar[bool] = ContextVar("boss_identity_enforced", default=False)

_UNSET = object()


def _authenticate(creds: authn.RequestCredentials, conn: Optional[HTTPConnection] = None):
    """Authenticate the request ONCE. The result (or the refusal) is kept on `request.state`, so the
    handler, the legacy transport middleware and the guard all see the same AuthContext and a
    DPoP proof or a session touch is never spent twice (S1-4)."""
    if conn is not None:
        cached = getattr(conn.state, "identity", None)
        if cached is not None:
            return cached
        failed = getattr(conn.state, "identity_error", None)
        if failed is not None:
            raise failed
        if getattr(conn.state, "identity_checked", False):
            return None
    try:
        try:
            ctx = authn.authenticate(creds)
        except NotImplementedError:
            # Until S1 lands, the only identity a request has is what the legacy transport decided.
            ctx = legacy.bridge(creds)
    except authn.AuthnError as exc:
        if conn is not None:
            conn.state.identity_error = exc
        raise
    if conn is not None:
        conn.state.identity_checked = True
        if ctx is not None:
            conn.state.identity = ctx
            conn.state.identity_ctx = ctx   # S1's handler cache name
    return ctx


def request_auth(conn: HTTPConnection):
    """The request's verified AuthContext (None when it carries no credential). Handlers call this;
    it never authenticates a second time. Raises AuthnError for a refused credential."""
    return _authenticate(_credentials(conn), conn)


def identity_enforced() -> bool:
    """Did the guard decide this request under `enforce` (and allow it)?"""
    return IDENTITY_ENFORCED.get()


async def enforce_access(conn: HTTPConnection) -> None:
    """The dependency. Never raises in shadow mode; in enforce mode raises AccessRefused."""
    route = conn.scope.get("route")
    endpoint = getattr(route, "endpoint", None) or conn.scope.get("endpoint")
    settings = get_settings()
    router_key = router_key_for(endpoint)
    mode = mode_for(router_key, global_mode=settings.authz_mode,
                    router_modes=settings.authz_router_modes)
    if mode == AuthzMode.OFF:
        return
    access = access_of(endpoint)
    body: Any = None
    if isinstance(access, Permit) and access.scope.body and isinstance(conn, Request):
        try:
            body = await conn.json()   # FastAPI already read and cached it; this is free
        except Exception:  # noqa: BLE001 — a missing/invalid body leaves the scope unresolved
            body = None
    creds = _credentials(conn)
    path_params = dict(conn.path_params)
    query = dict(conn.query_params)

    outcome: Outcome = await run_in_threadpool(
        evaluate, access,
        authenticate=lambda: _authenticate(creds, conn),
        values=lambda: RequestValues(path_params=path_params, query=query, body=body),
        org_id=lambda ctx: ctx.principal.org_id,
        check=lambda ctx, permission, scope: authz.check(ctx, permission, scope),
        resolve=lambda spec, values, org: scope_module.resolve(spec, values, org_id=org),
    )
    conn.state.identity_mode = mode.value
    ctx = getattr(conn.state, "identity", None)
    if ctx is not None:
        # Who acts, for everything this request causes: jobs it enqueues carry the principal
        # (R8), delegated() reads it, and the tool registry's identity gate sees it.
        from boss_engine.identity import lineage

        lineage.enter(ctx)
        if not getattr(conn.state, "tool_auth_set", False):
            try:
                from boss_engine.tools.registry import CURRENT_TOOL_AUTH

                CURRENT_TOOL_AUTH.set(ctx)
                conn.state.tool_auth_set = True
            except Exception:  # noqa: BLE001 — the engine's tool layer is optional here
                pass
    if outcome.allowed:
        if mode == AuthzMode.ENFORCE and not isinstance(access, Public):
            IDENTITY_ENFORCED.set(True)
            conn.state.identity_enforced = True
        return
    method = conn.scope.get("method") or "WS"
    path_template = getattr(route, "path", None) or conn.scope.get("path", "")
    LEDGER.record(router_key=router_key, method=method, path_template=path_template,
                  outcome=outcome, mode=mode)
    if mode == AuthzMode.ENFORCE:
        if conn.scope.get("type") == "websocket":
            raise WebSocketException(code=1008, reason=outcome.error_body().get("error", ""))
        raise AccessRefused(outcome)


async def _render_refusal(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AccessRefused)
    headers = {"WWW-Authenticate": "Bearer"} if exc.outcome.status == 401 else None
    return JSONResponse(status_code=exc.outcome.status, content=exc.outcome.error_body(),
                        headers=headers)


def install(app: FastAPI) -> None:
    """Register the refusal renderer. The dependency itself is passed to FastAPI(...)."""
    app.add_exception_handler(AccessRefused, _render_refusal)


def route_inventory(app: FastAPI) -> list[RouteEntry]:
    """Every route the app serves, with its declaration (None = undeclared)."""
    entries: list[RouteEntry] = []
    for route in app.router.routes:
        if isinstance(route, APIRoute):
            methods = tuple(sorted(m for m in (route.methods or ()) if m != "HEAD"))
            entries.append(RouteEntry(methods, route.path, "http", router_key_for(route.endpoint),
                                      access_of(route.endpoint)))
        elif isinstance(route, (APIWebSocketRoute, WebSocketRoute)):
            entries.append(RouteEntry((), route.path, "websocket", router_key_for(route.endpoint),
                                      access_of(route.endpoint)))
        elif isinstance(route, Mount):
            entries.append(RouteEntry((), route.path, "mount", "mount",
                                      FRAMEWORK_DECLARATIONS.get(route.path)))
        elif isinstance(route, Route):
            methods = tuple(sorted(m for m in (route.methods or {"GET"}) if m != "HEAD"))
            entries.append(RouteEntry(methods, route.path, "framework", "framework",
                                      FRAMEWORK_DECLARATIONS.get(route.path)))
    return entries


def check_app_coverage(app: FastAPI, *, echo: Callable[[str], None] = print) -> CoverageReport:
    """The boot-time check. Raises UndeclaredRoutesError under BOSS_AUTHZ_COVERAGE=fail."""
    settings = get_settings()
    policy = coverage_policy(global_mode=settings.authz_mode, coverage=settings.authz_coverage)
    return check_coverage(route_inventory(app), policy, echo=echo)


# ---- what the legacy transport middleware (main.py zone L) asks of identity ---------------------------
#
# The legacy middleware runs BEFORE routing, on every /v1 request, and under BOSS_REQUIRE_API_AUTH=true it
# used to refuse anything without a legacy key. Two things must reach the guard instead:
#   * the identity routes that authenticate by what they are (@public on an identity router: sign-in,
#     SSO, SCIM, token exchange, JWKS) — S1-8, otherwise sign-in is unreachable on a default install;
#   * a request that carries a NEW credential (a session cookie, a boss_ key, a DPoP run token). It is
#     authenticated here once (the guard reuses the result), and the legacy checks see the principal's
#     legacy member id (the claimant who "is" ceo) — or, once the route is enforced, defer to the guard.

_APP: Optional[FastAPI] = None
_IDENTITY_PREFIXES = ("/v1/identity/", "/v1/auth/", "/scim/", "/saml/v2/", "/.well-known/")


def bind_app(app: FastAPI) -> None:
    global _APP
    _APP = app


def _matched_route(scope: dict) -> Any:
    from starlette.routing import Match

    if _APP is None:
        return None
    for route in _APP.router.routes:
        if isinstance(route, (APIRoute, APIWebSocketRoute)):
            match, _ = route.matches(scope)
            if match == Match.FULL:
                return route
    return None


def is_identity_public_route(scope: dict) -> bool:
    """Is this an identity router's @public route (it proves itself: password, signed assertion,
    SCIM bearer, a subject token)? Only identity paths are matched, so this costs nothing elsewhere."""
    path = scope.get("path", "")
    if not path.startswith(_IDENTITY_PREFIXES):
        return False
    route = _matched_route(scope)
    if route is None:
        return False
    return (router_key_for(route.endpoint).startswith("identity")
            and isinstance(access_of(route.endpoint), Public))


def route_mode(scope: dict) -> AuthzMode:
    """The authz mode of the route this request will reach (identity routers are always enforced)."""
    settings = get_settings()
    path = scope.get("path", "")
    if not (settings.authz_router_modes or "").strip():
        if path.startswith(_IDENTITY_PREFIXES):
            return mode_for("identity", global_mode=settings.authz_mode, router_modes="")
        return mode_for("main", global_mode=settings.authz_mode, router_modes="")
    route = _matched_route(scope)
    key = router_key_for(getattr(route, "endpoint", None)) if route is not None else "main"
    return mode_for(key, global_mode=settings.authz_mode, router_modes=settings.authz_router_modes)


def presents_identity_credential(conn: HTTPConnection) -> bool:
    """A session cookie, or an Authorization bearer that is a boss_ key or a run token (JWT). The
    legacy console key / env keys (opaque hex) are not identity credentials: the bridge maps them."""
    if conn.cookies.get(authn.SESSION_COOKIE):
        return True
    raw = conn.headers.get("authorization", "")
    scheme, _, value = raw.partition(" ")
    value = value.strip()
    if scheme.lower() not in ("bearer", "dpop") or not value:
        return False
    if authn.parse_api_key(value) is not None:
        return True
    return _is_run_token(value)


def _is_run_token(value: str) -> bool:
    """A BOSS run token's JOSE header says typ boss-run+jwt; a legacy OIDC bearer (also a JWT) does
    not, and stays with the legacy middleware until M5."""
    import base64
    import json

    if value.count(".") != 2 or not value.startswith("eyJ"):
        return False
    head = value.split(".", 1)[0]
    try:
        header = json.loads(base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)))
    except Exception:  # noqa: BLE001
        return False
    from boss_engine.identity import delegation

    return isinstance(header, dict) and header.get("typ") in (delegation.TYP_RUN, getattr(delegation, "TYP_ACTOR", "boss-actor+jwt"))


def legacy_member_for(ctx: Any) -> str:
    """The legacy member id the zone-L checks see for an identity principal: the seat it took over
    at the claim ("ceo"), a legacy system principal's member, else the principal id itself (which
    no legacy membership names, so legacy checks refuse it until its router is enforced)."""
    principal = ctx.principal
    if principal.legacy_member_ids:
        return str(principal.legacy_member_ids[0])
    return str(principal.id)
