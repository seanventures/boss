"""/v1/identity/github*: how BOSS reaches GitHub for builds (Setup › Repo). engine: identity/github_setup.py.

  GET    /v1/identity/github                        which way is active (app | token | as_me | none)
  POST   /v1/identity/github/app/manifest           start "Connect a GitHub App": the form to POST to GitHub
  GET    /v1/identity/github/app/created            GitHub's redirect after the App is created (state-bound)
  GET    /v1/identity/github/app/installed          GitHub's redirect after the App is installed (state-bound)
  GET    /v1/identity/github/app/installations      the installation picker
  POST   /v1/identity/github/app/installation       choose one
  POST   /v1/identity/github/app/verify             what GitHub actually granted (permissions, repositories)
  DELETE /v1/identity/github/app                    forget the App
  POST   /v1/identity/github/token                  the fallback: a fine-grained token for named repos
  DELETE /v1/identity/github/token
  POST   /v1/identity/github/token/migrate-env      move BOSS_GITHUB_TOKEN into the vault (then unread under enforce)

Sign in with GitHub (device flow, one published App; engine: identity/github_device.py):
  GET    /v1/identity/github/signin                 is it configured here, and am I signed in
  POST   /v1/identity/github/device/start           a code to type at github.com/login/device
  POST   /v1/identity/github/device/poll            pending | connected | expired | denied | failed
  GET    /v1/identity/github/reach                  repos I can push to where BOSS is installed; installs needed

Every credential lands in the vault and is used only by boss-egress; nothing here returns one.
"""
from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.db import begin
from boss_engine.identity import P, PublicReason, Scope, github_app, github_device, github_setup, permit, public
from boss_engine.identity.authn import SESSION_COOKIE, AuthnError, authenticate
from boss_engine.identity.authn_support import default_org_id, public_origin

from .identity_auth import credentials, current_context, refusal


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


class DevicePollBody(_Body):
    handle: str = Field(min_length=10, max_length=2000)


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


def _console_session(request: Request, db: Any):
    """The operator's session cookie, or nothing.

    An update redirect carries no state. A missing cookie is anonymous: the network bridge and an
    API key are not this session, so they are not asked. A cookie that was presented is checked
    (a forgery raises AuthnError)."""
    if not request.cookies.get(SESSION_COOKIE):
        return None
    return authenticate(credentials(request), db=db)


def _unauthenticated() -> JSONResponse:
    return JSONResponse(status_code=401, content={"error": "unauthenticated"},
                        headers={"Cache-Control": "no-store"})


_REPO_NAME = re.compile(r"^[a-z0-9_.-]+/[a-z0-9_.-]+$")


def _in_use_repos(db: Any) -> list[str]:
    """Active bindings an app builds from: the project's default repo, or one a component names.

    A source binding with no component is not in use (it can be removed). The query is isolated
    so an older schema still confirms the update."""
    nested = getattr(db, "begin_nested", None)
    point = nested() if callable(nested) else None
    try:
        rows = db.execute(text(
            "SELECT DISTINCT lower(b.full_name) FROM github_repository_bindings b "
            "WHERE b.status = 'active' AND ("
            "b.role = 'primary' OR EXISTS ("
            "SELECT 1 FROM app_components c WHERE c.binding_id = b.id)) ORDER BY 1")).scalars().all()
    except Exception:  # noqa: BLE001 — an older schema: no warning, the update still confirms
        if point is not None:
            point.rollback()
        return []
    if point is not None:
        point.commit()
    return [str(name) for name in rows if _REPO_NAME.match(str(name))]


def _lost_in_use(db: Any) -> list[str]:
    """In-use repos the installation can no longer reach. A GitHub miss confirms with no warning.

    The list is the stored installation's (installation_repositories), never the query's
    installation_id. Only full_name is kept; a token on the payload is not."""
    try:
        listed = github_setup.installation_repositories(db)
    except (github_setup.GitHubSetupError, github_app.GitHubRefused):
        return []
    reachable = set()
    for item in listed or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("full_name") or "").strip().lower()
        if _REPO_NAME.match(name):
            reachable.add(name)
    return [name for name in _in_use_repos(db) if name not in reachable][:20]


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
                from boss_engine.services import ci_gate

                # workflows: write only when a repository opted in ("Builds may change CI
                # workflows", Owner/Admin + step-up); otherwise the App never asks for it.
                # A person or an organisation? GitHub says (GET /users/{owner}); the two create
                # Apps on different pages (identity/github_urls.py).
                account = github_setup.resolve_owner(body.owner or None)
                return github_setup.manifest(origin=public_origin(db, ctx.principal.org_id) or "",
                                             person_id=ctx.principal.id, owner=body.owner or None,
                                             allow_workflows=ci_gate.any_repo_allows_workflows(db),
                                             owner_account=account)
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    # Public: GitHub's browser redirect after the App is created. The proof is a `state` this BOSS
    # signed for the person who started, purpose-bound, expiring in 10 minutes and spent once
    # (github_setup._check_state).
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

    # Install sends the signed state. setup_on_update does not: installation_id and setup_action
    # only. The id in the query is not stored (it can be spoofed; GitHub already saved the change).
    # Without a state, the console session cookie is the proof.
    @router.get("/v1/identity/github/app/installed")
    @public(reason=PublicReason.OAUTH_STATE_BOUND_CALLBACK, verifier=github_setup.complete_install)
    def app_installed(request: Request, installation_id: str = "", state: str = "", setup_action: str = ""):
        try:
            with begin() as db:
                if (state or "").strip():
                    github_setup.complete_install(installation_id=installation_id[:40], state=state[:600],
                                                  org_id=default_org_id(db), db=db)
                    return _back(db, github="app_connected")
                if _console_session(request, db) is None:
                    return _unauthenticated()
                lost = _lost_in_use(db)
                confirmed = {"github": "app_updated"}
                if lost:
                    confirmed["lost"] = ",".join(lost)
                return _back(db, **confirmed)
        except AuthnError as exc:
            return refusal(exc)
        except (github_setup.GitHubSetupError, github_app.GitHubRefused) as exc:
            with begin() as db:
                return _back(db, github="app_failed", reason=getattr(exc, "code", "failed"))

    @router.get("/v1/identity/github/app/installations")
    @permit(P.GITHUB_READ, scope=Scope.org())     # a read: lists the App's installations, changes nothing
    def app_installations(request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                return {"installations": github_setup.installations(db=db, org_id=ctx.principal.org_id)}
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    @router.post("/v1/identity/github/app/verify")
    @permit(P.GITHUB_READ, scope=Scope.org())     # asks GitHub, keeps its answer; changes no credential
    def app_verify(request: Request):
        """What GitHub actually granted the installation: permissions accepted or waiting, and
        the repositories it was installed on, each gap with the GitHub page that closes it."""
        try:
            with begin() as db:
                ctx = current_context(request, db)
                return github_setup.verify(db, ctx.principal.org_id)
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

    # ---- Sign in with GitHub (device flow) — a person's own connection, never the org's ----------------
    def _person(request: Request, db: Any):
        ctx = current_context(request, db)
        if not getattr(ctx, "is_human_session", False):
            raise github_app.GitHubRefused("people_only", 403)
        return ctx

    @router.get("/v1/identity/github/signin")
    @permit(P.CONNECTION_USE_OWN, scope=Scope.org())
    def signin_status(request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                return github_device.status(ctx.principal.id, db=db)
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    @router.post("/v1/identity/github/device/start")
    @permit(P.CONNECTION_USE_OWN, scope=Scope.org())
    def device_start(request: Request):
        try:
            with begin() as db:
                ctx = _person(request, db)
            return github_device.start(ctx.principal.id)
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    @router.post("/v1/identity/github/device/poll")
    @permit(P.CONNECTION_USE_OWN, scope=Scope.org())
    def device_poll(body: DevicePollBody, request: Request):
        try:
            with begin() as db:
                ctx = _person(request, db)
                out = github_device.poll(person_id=ctx.principal.id, org_id=ctx.principal.org_id,
                                         handle=body.handle, db=db)
            return {k: v for k, v in out.items() if k != "item"}
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    @router.get("/v1/identity/github/reach")
    @permit(P.CONNECTION_USE_OWN, scope=Scope.org())
    def my_reach(request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                return github_device.reach(ctx.principal.id, db=db)
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    return router
