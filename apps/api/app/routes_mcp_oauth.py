"""OAuth 2.1 and device login for BOSS's own MCP server (BOSSAPP-862).

    GET  /.well-known/oauth-authorization-server   RFC 8414 metadata
    POST /v1/mcp-oauth/register                    RFC 7591 dynamic client registration (public)
    GET  /v1/mcp-oauth/authorize                   validates, then sends the browser to the
                                                   console's consent page
    POST /v1/mcp-oauth/consent                     the signed-in person's answer -> {redirect_to}
    POST /v1/mcp-oauth/token                       authorization_code (PKCE) | refresh_token | device_code
    POST /v1/mcp-oauth/device_authorization        RFC 8628 (public)
    POST /v1/mcp-oauth/device/approve              the signed-in person approves a terminal's code
    POST /v1/mcp-oauth/revoke                      RFC 7009 (public)
    GET  /v1/mcp-oauth/connected-apps              the caller's connected apps
    DELETE /v1/mcp-oauth/connected-apps/{grant_id} revoke one now

The issuer is BOSS_MCP_OAUTH_ISSUER, else BOSS_PUBLIC_URL, else the request's own origin.
Protocol errors are RFC 6749 bodies. Every write commits even when it answers an error (a spent
code, a revoked grant after refresh-token reuse stay spent / revoked).
"""
from __future__ import annotations

import os
from typing import Any, Callable, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from boss_engine.identity.declarations import PublicReason, authenticated, public


def verify_mcp_oauth_request(*_a: Any, **_k: Any) -> Any:
    """What protects the public OAuth endpoints (services/mcp_oauth_server): registered public
    clients only, exact redirect_uri match, PKCE S256 on every code, single-use 5-minute codes,
    hashed rotating refresh tokens with reuse revocation, device codes approved by a signed-in
    person. Tokens are BOSS personal API keys verified by identity like any other."""
    from boss_engine.services import mcp_oauth_server

    return mcp_oauth_server


OP = PublicReason.OIDC_PROVIDER_ENDPOINT


def _issuer(request: Request) -> str:
    configured = os.environ.get("BOSS_MCP_OAUTH_ISSUER") or os.environ.get("BOSS_PUBLIC_URL") or ""
    return (configured or str(request.base_url)).rstrip("/")


def _error(err: Any) -> JSONResponse:
    return JSONResponse(err.body(), status_code=err.status, headers={"Cache-Control": "no-store"})


async def _params(request: Request) -> dict[str, Any]:
    kind = request.headers.get("content-type", "")
    if "application/json" in kind:
        try:
            body = await request.json()
            return body if isinstance(body, dict) else {}
        except ValueError:
            return {}
    form = await request.form()
    return {k: v for k, v in form.items()}


def _ctx(request: Request):
    try:
        from apps.api.app import identity_guard

        return identity_guard.request_auth(request)
    except Exception:  # noqa: BLE001
        return None


def build_mcp_oauth_router(engine_getter: Optional[Callable[[], Any]] = None) -> APIRouter:
    from boss_engine.services import mcp_oauth_server as m

    router = APIRouter(tags=["mcp-oauth"])

    def _engine() -> Any:
        if engine_getter is not None:
            return engine_getter()
        from boss_engine.db import get_engine

        return get_engine()

    def _run(fn: Callable[[Any], Any]) -> Any:
        """Run in one transaction that COMMITS even when the protocol answers an error."""
        outcome: dict[str, Any] = {}
        with _engine().begin() as c:
            try:
                outcome["ok"] = fn(c)
            except m.OAuthError as err:
                outcome["err"] = err
        return _error(outcome["err"]) if "err" in outcome else outcome["ok"]

    @router.get("/.well-known/oauth-authorization-server")
    @public(reason=OP, verifier=verify_mcp_oauth_request)
    def oauth_metadata(request: Request) -> Any:
        return m.authorization_server_metadata(_issuer(request))

    @router.post("/v1/mcp-oauth/register", status_code=201)
    @public(reason=OP, verifier=verify_mcp_oauth_request)
    async def register(request: Request) -> Any:
        body = await _params(request)
        out = _run(lambda c: m.register_client(c, body))
        return JSONResponse(out, status_code=201) if isinstance(out, dict) else out

    @router.get("/v1/mcp-oauth/authorize")
    @public(reason=OP, verifier=verify_mcp_oauth_request)
    def authorize(request: Request, client_id: str = "", redirect_uri: str = "",
                  code_challenge: str = "", code_challenge_method: str = "",
                  response_type: str = "code", scope: Optional[str] = None,
                  state: Optional[str] = None) -> Any:
        def check(c: Any) -> Any:
            m.check_authorize_request(c, client_id=client_id, redirect_uri=redirect_uri,
                                      code_challenge=code_challenge,
                                      code_challenge_method=code_challenge_method,
                                      response_type=response_type, scope=scope)
            query = {k: v for k, v in (("client_id", client_id), ("redirect_uri", redirect_uri),
                                       ("code_challenge", code_challenge),
                                       ("code_challenge_method", code_challenge_method),
                                       ("scope", scope), ("state", state)) if v}
            return RedirectResponse(f"{_issuer(request)}/connected-apps/authorize?{urlencode(query)}",
                                    status_code=302)
        return _run(check)

    @router.post("/v1/mcp-oauth/consent")
    @authenticated(people_only=True, note="the signed-in person answers the consent screen")
    async def consent(request: Request) -> Any:
        ctx = _ctx(request)
        if ctx is None:
            return JSONResponse({"error": "unauthenticated"}, status_code=401)
        b = await _params(request)
        return _run(lambda c: {"redirect_to": m.authorize(
            c, principal_id=ctx.principal.id, org_id=ctx.principal.org_id,
            client_id=str(b.get("client_id") or ""), redirect_uri=str(b.get("redirect_uri") or ""),
            code_challenge=str(b.get("code_challenge") or ""),
            code_challenge_method=str(b.get("code_challenge_method") or "S256"),
            scope=b.get("scope"), state=b.get("state"), approve=bool(b.get("approve", True)))})

    @router.post("/v1/mcp-oauth/token")
    @public(reason=OP, verifier=verify_mcp_oauth_request)
    async def token(request: Request) -> Any:
        b = await _params(request)
        grant = str(b.get("grant_type") or "")
        client_id = str(b.get("client_id") or "")

        def issue(c: Any) -> Any:
            if grant == "authorization_code":
                return m.exchange_code(c, client_id=client_id, code=str(b.get("code") or ""),
                                       redirect_uri=str(b.get("redirect_uri") or ""),
                                       code_verifier=str(b.get("code_verifier") or ""))
            if grant == "refresh_token":
                return m.refresh(c, client_id=client_id, refresh_token=str(b.get("refresh_token") or ""))
            if grant == m.DEVICE_GRANT:
                return m.device_token(c, client_id=client_id, device_code=str(b.get("device_code") or ""))
            raise m.OAuthError("unsupported_grant_type", grant or "grant_type is required")
        out = _run(issue)
        return JSONResponse(out, headers={"Cache-Control": "no-store"}) if isinstance(out, dict) else out

    @router.post("/v1/mcp-oauth/device_authorization")
    @public(reason=OP, verifier=verify_mcp_oauth_request)
    async def device_authorization(request: Request) -> Any:
        b = await _params(request)
        return _run(lambda c: m.device_authorize(
            c, client_id=str(b.get("client_id") or ""), scope=b.get("scope"),
            verification_uri=f"{_issuer(request)}/connected-apps/device"))

    @router.post("/v1/mcp-oauth/device/approve")
    @authenticated(people_only=True, note="the signed-in person approves the code a terminal shows")
    async def device_approve(request: Request) -> Any:
        ctx = _ctx(request)
        if ctx is None:
            return JSONResponse({"error": "unauthenticated"}, status_code=401)
        b = await _params(request)
        return _run(lambda c: m.device_decide(c, user_code=str(b.get("user_code") or ""),
                                              principal_id=ctx.principal.id,
                                              org_id=ctx.principal.org_id,
                                              approve=bool(b.get("approve", True))))

    @router.post("/v1/mcp-oauth/revoke")
    @public(reason=OP, verifier=verify_mcp_oauth_request)
    async def revoke(request: Request) -> Any:
        b = await _params(request)
        _run(lambda c: m.revoke_token(c, client_id=str(b.get("client_id") or ""),
                                      token=str(b.get("token") or "")))
        return JSONResponse({}, status_code=200)

    @router.get("/v1/mcp-oauth/connected-apps")
    @authenticated(self_only=True)
    def connected_apps(request: Request) -> Any:
        ctx = _ctx(request)
        if ctx is None:
            return JSONResponse({"error": "unauthenticated"}, status_code=401)
        with _engine().begin() as c:
            return {"apps": m.connected_apps(c, ctx.principal.id)}

    @router.delete("/v1/mcp-oauth/connected-apps/{grant_id}")
    @authenticated(self_only=True)
    def revoke_connected_app(grant_id: str, request: Request) -> Any:
        ctx = _ctx(request)
        if ctx is None:
            return JSONResponse({"error": "unauthenticated"}, status_code=401)
        with _engine().begin() as c:
            done = m.revoke_grant(c, grant_id, principal_id=ctx.principal.id)
        return {"revoked": grant_id} if done else JSONResponse({"error": "not_found"}, status_code=404)

    return router
