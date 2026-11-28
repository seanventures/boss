"""Metatron CLI RFC 8628 device-code login and token lifecycle (/v1/auth/device/*, /token, /revoke).

Named `identity_*` so `identity_guard.is_identity_public_route` admits these @public routes
without legacy transport auth under `BOSS_REQUIRE_API_AUTH=true`. Unlike browser sign-in routes
in `identity_auth.py`, CLI device-login routes do NOT enforce browser `Origin` CSRF checks.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from boss_engine.identity.declarations import PublicReason, public


def verify_metatron_auth_request(*_a: Any, **_k: Any) -> Any:
    """Verifier for public Metatron device-login and token lifecycle routes.

    Protects endpoints via pre-registered/DCR OAuth client validation, RFC 8628 user-code
    approval by an authenticated person in the console, polling interval throttling (`slow_down`),
    single-use device codes, hashed rotating refresh tokens with RFC 9700 reuse revocation, and
    immediate `boss_pat_` credential revocation.
    """
    from boss_engine.services import mcp_oauth_server

    return mcp_oauth_server


def _issuer(request: Request) -> str:
    configured = os.environ.get("BOSS_MCP_OAUTH_ISSUER") or os.environ.get("BOSS_PUBLIC_URL") or ""
    return (configured or str(request.base_url)).rstrip("/")


async def _params(request: Request) -> dict[str, Any]:
    kind = request.headers.get("content-type", "")
    if "application/json" in kind:
        try:
            body = await request.json()
            return body if isinstance(body, dict) else {}
        except ValueError:
            return {}
    try:
        form = await request.form()
        return {k: v for k, v in form.items()}
    except Exception:  # noqa: BLE001
        return {}


def _no_store(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers={"Cache-Control": "no-store"})


def _principal_for_access_token(connection: Any, access_token: str) -> Optional[dict[str, Any]]:
    from boss_engine.identity.authn import hash_token
    from boss_engine.services import mcp_oauth_server as m

    principal_id = connection.execute(
        text("SELECT principal_id FROM identity_api_credentials WHERE token_hash = :h"),
        {"h": hash_token(access_token)},
    ).scalar()
    if not principal_id:
        return None
    return m.principal_summary(connection, str(principal_id))


def build_identity_metatron_auth_router(
    engine_getter: Optional[Callable[[], Any]] = None,
) -> APIRouter:
    from boss_engine.services import mcp_oauth_server as m

    router = APIRouter(tags=["metatron-auth"])

    def _engine() -> Any:
        if engine_getter is not None:
            return engine_getter()
        from boss_engine.db import get_engine

        return get_engine()

    def _run(fn: Callable[[Any], Any]) -> JSONResponse:
        """Run in a transaction that COMMITS even when an OAuthError is returned."""
        outcome: dict[str, Any] = {}
        with _engine().begin() as c:
            try:
                outcome["ok"] = fn(c)
            except m.OAuthError as err:
                outcome["err"] = err
        if "err" in outcome:
            err = outcome["err"]
            return _no_store(err.body(), status_code=err.status)
        return _no_store(outcome["ok"], status_code=200)

    @router.post("/v1/auth/device/code")
    @public(reason=PublicReason.AUTH_ENDPOINT, verifier=verify_metatron_auth_request)
    async def device_code(request: Request) -> JSONResponse:
        b = await _params(request)
        client_id = str(b.get("client_id") or m.METATRON_CLIENT_ID).strip() or m.METATRON_CLIENT_ID
        scope = b.get("scope") or "metatron"
        verification_uri = f"{_issuer(request)}/connected-apps/device"

        def _start(c: Any) -> dict[str, Any]:
            if client_id == m.METATRON_CLIENT_ID:
                m.ensure_metatron_client(c)
            return m.device_authorize(
                c,
                client_id=client_id,
                scope=str(scope),
                verification_uri=verification_uri,
            )

        return _run(_start)

    @router.post("/v1/auth/device/token")
    @public(reason=PublicReason.AUTH_ENDPOINT, verifier=verify_metatron_auth_request)
    async def device_token(request: Request) -> JSONResponse:
        b = await _params(request)
        grant_type = str(b.get("grant_type") or m.DEVICE_GRANT).strip()
        client_id = str(b.get("client_id") or m.METATRON_CLIENT_ID).strip() or m.METATRON_CLIENT_ID
        code = str(b.get("device_code") or "")

        def _exchange(c: Any) -> dict[str, Any]:
            if grant_type != m.DEVICE_GRANT:
                raise m.OAuthError("unsupported_grant_type", grant_type or "grant_type is required")
            if client_id == m.METATRON_CLIENT_ID:
                m.ensure_metatron_client(c)
            tokens = m.device_token(c, client_id=client_id, device_code=code)
            summary = _principal_for_access_token(c, tokens["access_token"])
            if summary is not None:
                tokens["principal"] = summary
            return tokens

        return _run(_exchange)

    @router.post("/v1/auth/token")
    @public(reason=PublicReason.AUTH_ENDPOINT, verifier=verify_metatron_auth_request)
    async def refresh_token(request: Request) -> JSONResponse:
        b = await _params(request)
        grant_type = str(b.get("grant_type") or "").strip()
        client_id = str(b.get("client_id") or m.METATRON_CLIENT_ID).strip() or m.METATRON_CLIENT_ID

        def _exchange(c: Any) -> dict[str, Any]:
            if client_id == m.METATRON_CLIENT_ID:
                m.ensure_metatron_client(c)
            if grant_type == "refresh_token":
                tokens = m.refresh(
                    c, client_id=client_id, refresh_token=str(b.get("refresh_token") or "")
                )
            elif grant_type == m.DEVICE_GRANT:
                tokens = m.device_token(
                    c, client_id=client_id, device_code=str(b.get("device_code") or "")
                )
            elif grant_type == "authorization_code":
                tokens = m.exchange_code(
                    c,
                    client_id=client_id,
                    code=str(b.get("code") or ""),
                    redirect_uri=str(b.get("redirect_uri") or ""),
                    code_verifier=str(b.get("code_verifier") or ""),
                )
            else:
                raise m.OAuthError("unsupported_grant_type", grant_type or "grant_type is required")
            summary = _principal_for_access_token(c, tokens["access_token"])
            if summary is not None:
                tokens["principal"] = summary
            return tokens

        return _run(_exchange)

    @router.post("/v1/auth/revoke")
    @public(reason=PublicReason.AUTH_ENDPOINT, verifier=verify_metatron_auth_request)
    async def revoke(request: Request) -> JSONResponse:
        b = await _params(request)
        explicit_client = b.get("client_id")
        client_id = str(explicit_client or m.METATRON_CLIENT_ID).strip() or m.METATRON_CLIENT_ID
        token = str(b.get("token") or "").strip()
        if not token:
            auth_header = request.headers.get("authorization") or ""
            if auth_header.lower().startswith("bearer "):
                token = auth_header[7:].strip()
            elif request.headers.get("x-boss-api-key"):
                token = str(request.headers.get("x-boss-api-key") or "").strip()

        def _revoke(c: Any) -> dict[str, Any]:
            if not token:
                return {"revoked": False}
            if client_id == m.METATRON_CLIENT_ID:
                m.ensure_metatron_client(c)
            m.revoke_token(c, client_id=client_id, token=token)
            if not explicit_client:
                # Also allow revoking a grant/PAT regardless of client_id when caller omits it.
                grant_id = c.execute(
                    text(
                        "SELECT id FROM mcp_oauth_grants WHERE revoked_at IS NULL AND "
                        "(refresh_hash = :h OR api_key_id IN "
                        " (SELECT id FROM identity_api_credentials WHERE token_hash = :th))"
                    ),
                    {"h": m._hash(token), "th": m._key_hash(token)},
                ).scalar()
                if grant_id:
                    m.revoke_grant(c, str(grant_id), reason="revoked by the client")
                elif token.startswith("boss_pat_"):
                    c.execute(
                        text(
                            "UPDATE identity_api_credentials SET revoked_at = now() "
                            "WHERE token_hash = :th AND revoked_at IS NULL"
                        ),
                        {"th": m._key_hash(token)},
                    )
            return {"revoked": True}

        return _run(_revoke)

    return router
