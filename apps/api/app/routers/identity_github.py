"""/v1/identity/github*: how BOSS reaches GitHub for builds (Setup › Repo). engine: identity/github_setup.py.

  GET    /v1/identity/github                        which way is active (app | token | as_me | none)
  POST   /v1/identity/github/app/manifest           start "Connect a GitHub App": the form to POST to GitHub
  GET    /v1/identity/github/app/created            GitHub's redirect after the App is created (state-bound)
  GET    /v1/identity/github/app/installed          GitHub's redirect after the App is installed (state-bound)
  GET    /v1/identity/github/app/installations      the installation picker
  POST   /v1/identity/github/app/installation       choose one
  DELETE /v1/identity/github/app                    forget the App
  POST   /v1/identity/github/token                  the fallback: a fine-grained token for named repos
  DELETE /v1/identity/github/token
  POST   /v1/identity/github/token/migrate-env      move BOSS_GITHUB_TOKEN into the vault (then unread under enforce)

Every credential lands in the vault and is used only by boss-egress; nothing here returns one.
"""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.config import get_settings
from boss_engine.db import begin
from boss_engine.identity import P, PublicReason, Scope, github_app, github_setup, permit, public
from boss_engine.identity.authn import AuthnError
from boss_engine.identity.authn_support import default_org_id, public_origin

from .identity_auth import current_context, refusal


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManifestBody(_Body):
    owner: Optional[str] = Field(default=None, max_length=100, pattern=r"^[A-Za-z0-9-]*$")


class InstallationBody(_Body):
    installation_id: str = Field(max_length=40)


class TokenBody(_Body):
    token: str = Field(min_length=10, max_length=400)
    repos: list[str] = Field(min_length=1, max_length=100)


class MigrateBody(_Body):
    repos: list[str] = Field(min_length=1, max_length=100)


def _err(exc: Exception) -> JSONResponse:
    if isinstance(exc, github_setup.GitHubSetupError):
        return JSONResponse(status_code=exc.status, content=exc.body())
    if isinstance(exc, github_app.GitHubRefused):
        return JSONResponse(status_code=exc.status, content={"error": exc.code})
    if isinstance(exc, AuthnError):
        return refusal(exc)
    raise exc


def _back(db: Any, **query: str) -> RedirectResponse:
    origin = public_origin(db) or ""
    return RedirectResponse(f"{origin}/?{urlencode({'area': 'setup', 'focus': 'repository', **query})}",
                            status_code=303)


def build_identity_github_router() -> APIRouter:
    router = APIRouter(tags=["identity"])

    @router.get("/v1/identity/github")
    @permit(P.GITHUB_READ, scope=Scope.org())
    def github_status(request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                return github_setup.mode(db, ctx.principal.org_id)
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    @router.post("/v1/identity/github/app/manifest")
    @permit(P.GITHUB_CONNECT, scope=Scope.org())
    def app_manifest(body: ManifestBody, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                return github_setup.manifest(origin=public_origin(db, ctx.principal.org_id) or "",
                                             person_id=ctx.principal.id, owner=body.owner or None)
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    # Public: GitHub's browser redirects. The proof is a `state` this BOSS signed for the person who
    # started, purpose-bound, expiring in 10 minutes and spent once (github_setup._check_state).
    @router.get("/v1/identity/github/app/created")
    @public(reason=PublicReason.OAUTH_STATE_BOUND_CALLBACK, verifier=github_setup.complete_manifest)
    def app_created(code: str = "", state: str = ""):
        try:
            with begin() as db:
                out = github_setup.complete_manifest(code=code[:200], state=state[:600],
                                                     org_id=default_org_id(db), db=db)
            return RedirectResponse(out["install_url"], status_code=303)
        except (github_setup.GitHubSetupError, github_app.GitHubRefused) as exc:
            with begin() as db:
                return _back(db, github="app_failed", reason=getattr(exc, "code", "failed"))

    @router.get("/v1/identity/github/app/installed")
    @public(reason=PublicReason.OAUTH_STATE_BOUND_CALLBACK, verifier=github_setup.complete_install)
    def app_installed(installation_id: str = "", state: str = "", setup_action: str = ""):
        try:
            with begin() as db:
                github_setup.complete_install(installation_id=installation_id[:40], state=state[:600] or None,
                                              org_id=default_org_id(db), db=db)
                return _back(db, github="app_connected")
        except (github_setup.GitHubSetupError, github_app.GitHubRefused) as exc:
            with begin() as db:
                return _back(db, github="app_failed", reason=getattr(exc, "code", "failed"))

    @router.get("/v1/identity/github/app/installations")
    @permit(P.GITHUB_CONNECT, scope=Scope.org())
    def app_installations(request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                return {"installations": github_setup.installations(db=db, org_id=ctx.principal.org_id)}
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    @router.post("/v1/identity/github/app/installation")
    @permit(P.GITHUB_CONNECT, scope=Scope.org())
    def app_installation(body: InstallationBody, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                return github_setup.select_installation(installation_id=body.installation_id,
                                                        org_id=ctx.principal.org_id, db=db, by=ctx.principal.id)
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    @router.delete("/v1/identity/github/app")
    @permit(P.GITHUB_CONNECT, scope=Scope.org())
    def app_disconnect(request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                github_setup.disconnect_app(org_id=ctx.principal.org_id, db=db, by=ctx.principal.id)
                return {"disconnected": True}
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    @router.post("/v1/identity/github/token")
    @permit(P.GITHUB_CONNECT, scope=Scope.org())
    def org_token_store(body: TokenBody, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                out = github_setup.store_org_token(token=body.token, repos=body.repos, org_id=ctx.principal.org_id,
                                                   db=db, by=ctx.principal.id)
                return {k: v for k, v in out.items() if k != "id"}
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    @router.delete("/v1/identity/github/token")
    @permit(P.GITHUB_CONNECT, scope=Scope.org())
    def org_token_remove(request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                return {"removed": github_setup.remove_org_token(org_id=ctx.principal.org_id, db=db,
                                                                 by=ctx.principal.id)}
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    @router.post("/v1/identity/github/token/migrate-env")
    @permit(P.GITHUB_CONNECT, scope=Scope.org())
    def org_token_migrate(body: MigrateBody, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                out = github_setup.migrate_env_token(repos=body.repos, org_id=ctx.principal.org_id, db=db,
                                                     by=ctx.principal.id, settings=get_settings())
                return {k: v for k, v in out.items() if k != "id"}
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    return router
