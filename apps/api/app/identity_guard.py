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
from boss_engine.identity.declarations import Access, Authenticated, Permit, access_of
from boss_engine.identity.enforcement import (
    LEDGER,
    AuthzMode,
    CoverageReport,
    Outcome,
    RouteEntry,
    check_coverage,
    coverage_policy,
    evaluate,
    evaluate_object_rules,
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


#: The legacy worker check (main.py: transport auth off, or the principal is BOSS_WORKER_PRINCIPAL).
#: Default refuses: an API that never configured it has no legacy worker.
_legacy_worker_ok: Callable[[], bool] = lambda: False  # noqa: E731


def configure(*, legacy_member: Callable[[], Optional[str]],
              legacy_method: Callable[[], Optional[str]],
              legacy_worker_ok: Optional[Callable[[], bool]] = None) -> None:
    global _legacy_member, _legacy_method, _legacy_worker_ok
    _legacy_member, _legacy_method = legacy_member, legacy_method
    if legacy_worker_ok is not None:
        _legacy_worker_ok = legacy_worker_ok


def is_worker_principal(ctx: Any) -> bool:
    """A system principal whose fixed set holds worker:lease: sys_worker, or the legacy worker
    key's principal while that bridge lives. Never a person, a key of a person, or an agent run."""
    from boss_engine.identity.permissions_gen import P
    from boss_engine.identity.principal import PrincipalKind

    if ctx is None or ctx.principal.kind != PrincipalKind.SYSTEM or ctx.run_token is not None:
        return False
    try:
        return P.WORKER_LEASE in authz.system_permissions(ctx.principal.id)
    except Exception:  # noqa: BLE001 — cannot tell: not a worker
        return False


def require_worker_principal() -> None:
    """THE worker-protocol check, for every handler of the worker protocol (claim, heartbeat,
    complete, fail, apply, execute, tick): only the worker's own principal may call it.

    Enforced route: the request's verified principal must be a worker principal — never taken on
    trust from the route's declaration. Shadow/off: an identity worker principal passes, and
    otherwise the legacy rule decides (BOSS_WORKER_PRINCIPAL, or transport auth off in local
    development). Raises HTTPException 403."""
    from fastapi import HTTPException

    ctx = REQUEST_IDENTITY.get()
    if is_worker_principal(ctx):
        return
    if identity_enforced() or not _legacy_worker_ok():
        raise HTTPException(status_code=403, detail={
            "error": "forbidden", "permission": "worker:lease",
            "message": "only the worker principal may call the worker protocol"})


def router_key_for(endpoint: Any) -> str:
    """The rollout unit: the endpoint's module tail (`main`, `setup`, `identity_auth`, ...)."""
    module = getattr(endpoint, "__module__", "") or ""
    return module.rsplit(".", 1)[-1] or "unknown"


def _credentials(conn: HTTPConnection) -> authn.RequestCredentials:
    client = conn.client.host if conn.client else None
    # F2: the PEER is the socket that connected (apps/api/serve.py records it before the forwarded
    # headers rewrite scope["client"]); trusted-proxy decisions must never read a forwarded value.
    peer = conn.scope.get("boss.raw_peer") or client
    return authn.RequestCredentials(
        method=conn.scope.get("method", "WEBSOCKET"),
        path=conn.scope.get("path", ""),
        headers={k.lower(): v for k, v in conn.headers.items()},
        cookies=dict(conn.cookies),
        client_ip=client,
        peer_ip=peer,
        request_id=conn.headers.get("x-request-id"),
        legacy_member_id=_legacy_member(),
        legacy_auth_method=_legacy_method(),
    )


#: Set for the rest of a request once the guard has ALLOWED it under `enforce`: the decision was
#: made here, against the catalogue, so main.py's legacy actor/member checks (zone L) defer to it.
#: Never set in shadow mode, where the legacy checks still decide.
IDENTITY_ENFORCED: ContextVar[bool] = ContextVar("boss_identity_enforced", default=False)

_UNSET = object()

#: The request's authz mode ("off" | "shadow" | "enforce") and its verified AuthContext, for code
#: deeper than the handler (a helper with no `request`): identity_access.require_production reads
#: them. Set by the guard on every request it decides.
IDENTITY_MODE: ContextVar[str] = ContextVar("boss_identity_mode", default="")
REQUEST_IDENTITY: ContextVar[Any] = ContextVar("boss_request_identity", default=None)


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
    if isinstance(access, (Permit, Authenticated)) and isinstance(conn, Request) and (
            (isinstance(access, Permit) and _reads_body(access)) or _json_body_expected(conn, route)):
        try:
            # FastAPI already read and cached it; this is free — and it is the SAME object FastAPI
            # validates the handler's body from, which is what lets the actor rule below replace a
            # claimed actor before the handler sees it.
            body = await conn.json()
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
    IDENTITY_MODE.set(mode.value)
    REQUEST_IDENTITY.set(ctx)
    if outcome.allowed and isinstance(access, Permit) and ctx is not None:
        # The object rules (object_rules.py): the declaration's confined ids and production
        # target, AND the central rule (F2 / M5, M9): every id the request names by an object-id
        # name — path, query or body at any depth — is in the project the permission was checked
        # at (the org, for an org-level check), and every org_id is the caller's org.
        decided = outcome.decision.scope if outcome.decision is not None else None
        if decided is not None:
            refused = await run_in_threadpool(
                evaluate_object_rules, access, ctx, decided,
                RequestValues(path_params=path_params, query=query, body=body),
                check=lambda c, permission, scope: authz.check(c, permission, scope),
                named_skip=(access.scope.location,) if access.scope.location else (),
                org_locations=_org_id_locations(route))
            if refused is not None:
                outcome = refused
    if not outcome.allowed and outcome.authn_code == authn.ENROLMENT_REQUIRED:
        # H1, not a mode question: an enrolment-only session (MFA required, no factor yet) is
        # refused everything but enrolment, sign-out and "me" in shadow too.
        method = conn.scope.get("method") or "WS"
        path_template = getattr(route, "path", None) or conn.scope.get("path", "")
        LEDGER.record(router_key=router_key, method=method, path_template=path_template,
                      outcome=outcome, mode=AuthzMode.ENFORCE)
        if conn.scope.get("type") == "websocket":
            raise WebSocketException(code=1008, reason=authn.ENROLMENT_REQUIRED)
        raise AccessRefused(outcome)
    csrf = _writing_get_csrf(conn, access, ctx, creds)
    if csrf is not None:
        # Not a mode question: like the CSRF check on every cookie write, a forged GET that
        # writes is refused in shadow too (only `off`, the rollback switch, skips it).
        method = conn.scope.get("method") or "GET"
        path_template = getattr(route, "path", None) or conn.scope.get("path", "")
        LEDGER.record(router_key=router_key, method=method, path_template=path_template,
                      outcome=csrf, mode=AuthzMode.ENFORCE)
        raise AccessRefused(csrf)
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
            if ctx is not None:
                _bind_actor(conn, route, ctx, body)
        return
    method = conn.scope.get("method") or "WS"
    path_template = getattr(route, "path", None) or conn.scope.get("path", "")
    LEDGER.record(router_key=router_key, method=method, path_template=path_template,
                  outcome=outcome, mode=mode)
    if mode == AuthzMode.ENFORCE:
        if conn.scope.get("type") == "websocket":
            raise WebSocketException(code=1008, reason=outcome.error_body().get("error", ""))
        raise AccessRefused(outcome)


# ---- THE ACTOR RULE (F2 / H7): under enforce, who acts is the verified principal, always ----------
#
# Hundreds of routes take a client-supplied `actor` (body field or query parameter) and handlers
# used it for history, ownership filters (agent threads listed "by actor") and legacy member checks.
# Under enforce those legacy checks return early — so a caller could name any actor and read or
# continue someone else's thread. One rule, here, before any handler runs: on an enforced, allowed
# request the claimed actor is REPLACED by the request's own actor (request_actor), in the body
# FastAPI validates from and in the query string. A difference is recorded
# (legacy.claimed_actor_ignored) but never decides anything (R6).
#
# The one exception is the worker principal: it replays work on behalf of the agent persona a job
# names (shepherd, metatron, system) and holds no person's identity to substitute.

ACTOR_FIELD = "actor"
REQUEST_ACTOR: ContextVar[Optional[str]] = ContextVar("boss_request_actor", default=None)


def actor_for(ctx: Any) -> str:
    """The actor string for a verified principal: the legacy member it took over (the claimant
    who "is" ceo, the console key's `executive_console`), else its own principal id."""
    principal = ctx.principal
    if principal.legacy_member_ids:
        return str(principal.legacy_member_ids[0])
    return str(principal.id)


def request_actor() -> Optional[str]:
    """The enforced request's actor (None when the guard did not enforce this request)."""
    return REQUEST_ACTOR.get() if identity_enforced() else None


def actor_is_a_filter(fn: Any) -> Any:
    """Mark a route whose `actor` query parameter SELECTS rows by actor (the audit log's "who did
    it" filter) rather than claiming who acts. The actor rule leaves such a parameter alone; the
    route's own permission (audit:read) already decides what may be filtered."""
    fn._boss_actor_is_filter = True
    return fn


def _route_takes_actor(route: Any) -> tuple[bool, bool]:
    """(body has an `actor` field, query has an `actor` parameter) for a route; cached."""
    cached = getattr(route, "_boss_actor_params", None)
    if cached is not None:
        return cached
    in_body = in_query = False
    if getattr(getattr(route, "endpoint", None), "_boss_actor_is_filter", False):
        try:
            route._boss_actor_params = (False, False)
        except Exception:  # noqa: BLE001
            pass
        return False, False
    dependant = getattr(route, "dependant", None)
    if dependant is not None:
        in_query = any((p.alias or p.name) == ACTOR_FIELD for p in dependant.query_params)
        for p in dependant.body_params:
            fields = getattr(getattr(p.field_info, "annotation", None), "model_fields", None) or {}
            if (p.alias or p.name) == ACTOR_FIELD or any(
                    (f.alias or k) == ACTOR_FIELD for k, f in fields.items()):
                in_body = True
    try:
        route._boss_actor_params = (in_body, in_query)
    except Exception:  # noqa: BLE001
        pass
    return in_body, in_query


def _bind_actor(conn: HTTPConnection, route: Any, ctx: Any, body: Any) -> None:
    actor = actor_for(ctx)
    REQUEST_ACTOR.set(actor)
    conn.state.identity_actor = actor
    if is_worker_principal(ctx):
        return
    claimed: list[str] = []
    body_field, query_param = _route_takes_actor(route)
    if isinstance(body, dict) and (ACTOR_FIELD in body or body_field):
        if body.get(ACTOR_FIELD) not in (None, actor):
            claimed.append(str(body.get(ACTOR_FIELD)))
        body[ACTOR_FIELD] = actor            # the same dict FastAPI validates the handler's body from
    query_string = conn.scope.get("query_string", b"") or b""
    if getattr(getattr(route, "endpoint", None), "_boss_actor_is_filter", False):
        return                                    # `actor` here is a filter, not a claim
    if query_param or b"actor=" in query_string:
        from urllib.parse import parse_qsl, urlencode

        pairs = parse_qsl(query_string.decode("latin-1"), keep_blank_values=True)
        claimed += [v for k, v in pairs if k == ACTOR_FIELD and v != actor]
        if query_param or any(k == ACTOR_FIELD for k, _v in pairs):
            pairs = [(k, v) for k, v in pairs if k != ACTOR_FIELD] + [(ACTOR_FIELD, actor)]
            conn.scope["query_string"] = urlencode(pairs).encode("latin-1")
            if hasattr(conn, "_query_params"):
                del conn._query_params       # Starlette caches it; FastAPI reads it after us
    for value in claimed[:1]:
        try:
            legacy.note_claimed_actor(ctx, value)
        except Exception:  # noqa: BLE001 — recording the difference never blocks the request
            pass


def _org_id_locations(route: Any) -> tuple[str, ...]:
    """Where this route READS an `org_id` (path, query, a body field); cached per route."""
    cached = getattr(route, "_boss_org_id_locations", None)
    if cached is not None:
        return cached
    out: list[str] = []
    dependant = getattr(route, "dependant", None)
    if dependant is not None:
        if any((p.alias or p.name) == "org_id" for p in dependant.path_params):
            out.append("path")
        if any((p.alias or p.name) == "org_id" for p in dependant.query_params):
            out.append("query")
        for p in dependant.body_params:
            fields = getattr(getattr(p.field_info, "annotation", None), "model_fields", None) or {}
            if (p.alias or p.name) == "org_id" or any((f.alias or k) == "org_id"
                                                      for k, f in fields.items()):
                out.append("body")
    else:   # no dependant to read (a stub): every location
        out = ["path", "query", "body"]
    result = tuple(out)
    try:
        route._boss_org_id_locations = result
    except Exception:  # noqa: BLE001
        pass
    return result


def _json_body_expected(conn: HTTPConnection, route: Any) -> bool:
    """Read the JSON body for the central rules: a body-carrying method whose content is JSON
    (or unlabelled, which FastAPI parses as JSON too). Never a form or an upload."""
    if (conn.scope.get("method") or "GET").upper() in ("GET", "HEAD", "OPTIONS"):
        return False
    kind = (conn.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if not kind:
        return bool(getattr(getattr(route, "dependant", None), "body_params", None))
    return kind == "application/json" or kind.endswith("+json")


#: A writing GET's POST twin lives at `<path>/load` (several of these paths already answer a
#: different POST, so the twin cannot always share the path).
WRITING_GET_TWIN_SUFFIX = "/load"
_TWINS: set[tuple[int, str]] = set()


def writing_get_twin(path: str) -> str:
    return path.rstrip("/") + WRITING_GET_TWIN_SUFFIX


def writing_get(registrar: Any, path: str, **kwargs: Any) -> Callable[[Any], Any]:
    """Register a GET that WRITES (it seeds defaults, reaps, freezes or probes): the GET stays as a
    deprecated alias and the same endpoint, with the same declaration, answers POST at
    `writing_get_twin(path)` — the method callers move to. The endpoint must be declared
    `@permit(..., writes_on_get=True)` (below this decorator), which makes the guard treat a
    cookie-authenticated GET to it as a write for CSRF."""
    def deco(fn: Any) -> Any:
        access = access_of(fn)
        if not (isinstance(access, Permit) and access.writes_on_get):
            raise RuntimeError(f"{getattr(fn, '__qualname__', fn)}: writing_get needs "
                               "@permit(..., writes_on_get=True) below it")
        registrar.get(path, deprecated=True, **kwargs)(fn)
        twin = writing_get_twin(path)
        key = (id(registrar), twin)
        if key not in _TWINS:
            _TWINS.add(key)
            registrar.post(twin, **kwargs)(fn)
        return fn
    return deco


def _reads_body(access: Permit) -> bool:
    """Does this declaration read the JSON body (its scope, a confined id or a production source)?"""
    if access.scope.body:
        return True
    if any(rule.where == "body" for rule in access.confine):
        return True
    production = access.production
    return production is not None and any(where == "body" for _k, where, _n in production._sources)


def _writing_get_csrf(conn: HTTPConnection, access: Any, ctx: Any,
                      creds: authn.RequestCredentials) -> Optional[Outcome]:
    """A cookie-authenticated GET to a `writes_on_get` route without proof it came from the
    console's own page: the refusal Outcome (403 csrf_failed), else None."""
    if not (isinstance(access, Permit) and access.writes_on_get):
        return None
    if (conn.scope.get("method") or "").upper() not in ("GET", "HEAD"):
        return None
    if access.writes_on_get_if and not any(
            conn.query_params.get(name) in values for name, values in access.writes_on_get_if):
        return None    # this GET, with these parameters, only reads
    from boss_engine.identity.principal import AuthMethod

    if ctx is None or ctx.method != AuthMethod.SESSION or ctx.session is None:
        return None
    try:
        authn.check_writing_get_csrf(creds, session_id=ctx.session.id)
    except authn.AuthnError as exc:
        return Outcome(False, authz.DenyReason.UNAUTHENTICATED, access.permission, access.describe(),
                       ctx.principal.kind.value, detail=f"authn: {exc.code}",
                       authn_status=exc.status, authn_code=exc.code)
    return None


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
        # a boss_ key in X-Boss-Api-Key (the rc5 host tools' header) is an identity credential too
        return authn.api_key_header({k.lower(): v for k, v in conn.headers.items()}) is not None
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


def legacy_member_for(ctx: Any, *, console_member: Optional[str] = None) -> str:
    """The legacy member id the zone-L checks see for an identity principal (shadow mode, where
    the legacy checks still decide):
      - the seat it took over at the claim ("ceo");
      - the shared-password and network-bridge principals, and an org Owner, act as the member the
        console's own key acted as before identity (`console_member`, `ceo` on an install) —
        "the console behaves exactly as today until the routers are enforced"; an Owner holds
        every permission in the new model, so this widens nothing;
      - anyone else: the principal id itself, which no legacy membership names, so the legacy
        checks refuse it until its router is enforced (a new person never gains reach in shadow).
    """
    from boss_engine.identity import legacy as legacy_module

    principal = ctx.principal
    if principal.legacy_member_ids:
        return str(principal.legacy_member_ids[0])
    if console_member and (
            principal.id in (legacy_module.LEGACY_SHARED_PRINCIPAL,
                             legacy_module.NETWORK_OPERATOR_PRINCIPAL)
            or _is_org_owner(principal)):
        return console_member
    return str(principal.id)


def _is_org_owner(principal: Any) -> bool:
    if getattr(principal, "kind", None) is None or principal.kind.value != "user":
        return False
    from boss_engine.db import begin
    from boss_engine.identity import bindings

    try:
        with begin() as db:
            return principal.id in bindings.active_owners(db, principal.org_id)
    except Exception:  # noqa: BLE001 — unknown is not an Owner
        return False
