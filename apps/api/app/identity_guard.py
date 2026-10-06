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
#: decorator; S2 declares them here by path. Phase 0 leaves them undeclared.
FRAMEWORK_DECLARATIONS: dict[str, Access] = {}


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


def _authenticate(creds: authn.RequestCredentials):
    try:
        return authn.authenticate(creds)
    except NotImplementedError:
        # Until S1 lands, the only identity a request has is what the legacy transport decided.
        return legacy.bridge(creds)


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
        authenticate=lambda: _authenticate(creds),
        values=lambda: RequestValues(path_params=path_params, query=query, body=body),
        org_id=lambda ctx: ctx.principal.org_id,
        check=lambda ctx, permission, scope: authz.check(ctx, permission, scope),
        resolve=lambda spec, values, org: scope_module.resolve(spec, values, org_id=org),
    )
    if outcome.allowed:
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
