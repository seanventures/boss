"""The login-oauth routes for a FastAPI app:

    GET  /auth/providers           the configured providers, for the login buttons
    GET  /auth/login/{provider}    redirect to the provider (state + PKCE S256 + nonce)
    GET  /auth/callback/{provider} check state, exchange the code, check the ID token, sign in
    POST /auth/logout              CSRF-checked; destroys the session
    GET  /api/me                   the signed-in user (401 when signed out)
    GET  /api/admin                an example route only the `admin` role may call

plus the dependencies `require_user` and `require_role("admin")` for the app's own routes.

    auth = create_auth(load_config())
    app.add_middleware(ServerSessionMiddleware, **auth.session_settings)
    app.include_router(auth.router)
"""
from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from typing import Any, Callable, Optional

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from .config import Config
from .sessions import rotate_session
from .users import AccountExistsError, User, UserStore


def _safe_return_to(value: Optional[str]) -> str:
    """Only a path on this app may be a post-login destination (no open redirect)."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def _same(a: Optional[str], b: Optional[str]) -> bool:
    return isinstance(a, str) and isinstance(b, str) and hmac.compare_digest(a.encode(), b.encode())


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status)


@dataclass
class Auth:
    router: APIRouter
    store: UserStore
    session_settings: dict
    require_user: Callable
    require_role: Callable[[str], Callable]
    require_csrf: Callable


def create_auth(config: Config, *, store: Optional[UserStore] = None, session_store: Any = None) -> Auth:
    store = store or UserStore(config.admin_emails)
    oauth = OAuth()
    for p in config.providers.values():
        # PKCE S256 on every provider, confidential clients included.
        kwargs: dict[str, Any] = {"scope": p.scope, "code_challenge_method": "S256"}
        if p.kind == "oidc":
            oauth.register(p.id, client_id=p.client_id, client_secret=p.client_secret,
                           server_metadata_url=f"{p.issuer}/.well-known/openid-configuration",
                           client_kwargs=kwargs)
        else:
            oauth.register(p.id, client_id=p.client_id, client_secret=p.client_secret,
                           authorize_url=p.authorize_url, access_token_url=p.token_url,
                           api_base_url=p.api_url, client_kwargs=kwargs)

    def current_user(request: Request) -> Optional[User]:
        return store.get(request.session.get("user_id"))

    def require_user(request: Request) -> User:
        user = current_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="signed_out")
        return user

    def require_role(role: str):
        def dependency(user: User = Depends(require_user)) -> User:
            if role not in store.roles_for(user):
                raise HTTPException(status_code=403, detail="forbidden")
            return user
        return dependency

    def require_csrf(request: Request) -> None:
        """State-changing requests made with the session cookie must carry the CSRF token
        /api/me hands out (same-origin JavaScript can read it; another site cannot)."""
        if not _same(request.headers.get("x-csrf-token"), request.session.get("csrf")):
            raise HTTPException(status_code=403, detail="csrf")

    router = APIRouter()

    @router.get("/auth/providers")
    def providers() -> dict:
        return {"providers": [{"id": p.id, "name": p.name, "login_url": f"/auth/login/{p.id}"}
                              for p in config.providers.values()]}

    @router.get("/auth/login/{provider}")
    async def login(provider: str, request: Request, return_to: Optional[str] = None):
        if provider not in config.providers:
            return _error(404, "unknown_provider")
        client = oauth.create_client(provider)
        redirect_uri = f"{config.base_url}/auth/callback/{provider}"
        try:
            rv = await client.create_authorization_url(redirect_uri)
        except Exception:  # noqa: BLE001 - discovery failed: the provider is unreachable
            return _error(502, "provider_unavailable")
        # Authlib keeps state, nonce and the PKCE verifier in the (server-side) session.
        await client.save_authorize_data(request, redirect_uri=redirect_uri, **rv)
        request.session["oauth"] = {"provider": provider, "state": rv["state"],
                                    "return_to": _safe_return_to(return_to)}
        return RedirectResponse(rv["url"], status_code=302)

    @router.get("/auth/callback/{provider}")
    async def callback(provider: str, request: Request):
        if provider not in config.providers:
            return _error(404, "unknown_provider")
        pending = request.session.pop("oauth", None)
        # One use, whatever happens next: drop every login attempt Authlib still holds.
        attempt_keys = [k for k in request.session if k.startswith(f"_state_{provider}_")]
        state = request.query_params.get("state")
        if not pending or pending.get("provider") != provider or not _same(state, pending.get("state")):
            for k in attempt_keys:
                request.session.pop(k, None)
            return _error(400, "invalid_state")
        if request.query_params.get("error"):
            for k in attempt_keys:
                request.session.pop(k, None)
            return _error(400, "provider_refused")
        client = oauth.create_client(provider)
        spec = config.providers[provider]
        try:
            token = await client.authorize_access_token(request)
            if spec.kind == "oidc":
                claims = token.get("userinfo")
                if not claims:  # an OIDC provider must answer with a checked ID token
                    raise OAuthError(description="no id_token")
                profile = {"subject": str(claims["sub"]), "email": claims.get("email"),
                           "email_verified": claims.get("email_verified") is True,
                           "name": claims.get("name"), "avatar": claims.get("picture")}
            else:
                user = (await client.get("user", token=token)).raise_for_status().json()
                emails_res = await client.get("user/emails", token=token)
                emails = emails_res.json() if emails_res.status_code == 200 else []
                primary = next((e for e in emails if e.get("primary")), None)
                profile = {"subject": str(user["id"]), "email": primary["email"] if primary else None,
                           "email_verified": bool(primary and primary.get("verified")),
                           "name": user.get("name") or user.get("login"), "avatar": user.get("avatar_url")}
        except Exception:  # noqa: BLE001 - every failure is the same fixed answer, never echoed
            return _error(400, "login_failed")
        finally:
            for k in attempt_keys:
                request.session.pop(k, None)
        try:
            user = store.sign_in(provider=provider, **profile)
        except AccountExistsError as exc:
            return _error(409, exc.code)
        # A fresh session id at sign-in (no session fixation), and a fresh CSRF token.
        request.session.clear()
        request.session.update({"user_id": user.id, "csrf": secrets.token_urlsafe(32)})
        rotate_session(request)
        return RedirectResponse(pending.get("return_to") or "/", status_code=302)

    @router.post("/auth/logout", status_code=204, dependencies=[Depends(require_csrf)])
    def logout(request: Request) -> Response:
        request.session.clear()
        return Response(status_code=204)

    @router.get("/api/me")
    def me(request: Request, user: User = Depends(require_user)) -> dict:
        return {"id": user.id, "email": user.email, "email_verified": user.email_verified,
                "name": user.name, "avatar": user.avatar, "providers": list(user.providers),
                "roles": store.roles_for(user), "csrf_token": request.session.get("csrf")}

    @router.get("/api/admin", dependencies=[Depends(require_role("admin"))])
    def admin() -> dict:
        return {"ok": True}

    settings = {"secret": config.session_secret, "https_only": config.secure_cookies,
                "store": session_store}
    auth = Auth(router=router, store=store, session_settings=settings, require_user=require_user,
                require_role=require_role, require_csrf=require_csrf)
    auth.current_user = current_user  # type: ignore[attr-defined]
    return auth


def error_body(exc: HTTPException) -> JSONResponse:
    """`{"error": "<code>"}` for the auth dependencies' 401/403 (main.py installs it)."""
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))


__all__ = ["Auth", "create_auth", "error_body"]
