"""/scim/v2/*: org-level SCIM 2.0 provisioning (IDENTITY.md §4.9). Stream S1.

Every route is `@permit(scim:provision)` — a system-only permission only `sys_scim_*` principals
hold — and the handler checks again that the request carries a `boss_scim_` key of such a
principal (defence in depth: a person's session or PAT never provisions). The project-path routes
(`/scim/v2/{project_id}/...`) in main.py stay as the one-release alias (S2/S5).
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

from sqlalchemy.exc import DBAPIError
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from boss_engine.db import begin
from boss_engine.identity import P, permit, scim
from boss_engine.identity.authn import AuthnError
from boss_engine.identity.principal import AuthContext, PrincipalKind

from .identity_auth import current_context

SCIM_MEDIA = "application/scim+json"
MAX_BODY = 256 * 1024


def _scim(status: int, body: dict[str, Any]) -> JSONResponse:
    return JSONResponse(status_code=status, content=body, media_type=SCIM_MEDIA,
                        headers={"Cache-Control": "no-store"})


def _error(status: int, detail: str, scim_type: Optional[str] = None) -> JSONResponse:
    return _scim(status, scim.ScimError(status, detail, scim_type).body())


def is_scim_caller(ctx: AuthContext) -> bool:
    return bool(ctx.credential and ctx.credential.kind == "scim"
                and ctx.principal.kind == PrincipalKind.SYSTEM
                and ctx.principal.id.startswith("sys_scim_")
                and "scim:provision" in ctx.credential.scopes)


async def _body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise scim.ScimError(413, "request too large")
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise scim.ScimError(400, "body must be JSON", "invalidSyntax") from None
    if not isinstance(data, dict):
        raise scim.ScimError(400, "body must be a JSON object", "invalidSyntax")
    return data


def _handle(request: Request, work: Callable[[Any, AuthContext], Any], status: int = 200) -> Response:
    try:
        with begin() as db:
            ctx = current_context(request, db)
            if not is_scim_caller(ctx):
                return _error(403, "a SCIM provisioning key is required")
            result = work(db, ctx)
        if result is None:
            return Response(status_code=204)
        return _scim(status, result)
    except AuthnError as exc:
        return _error(exc.status, exc.code)
    except scim.ScimError as exc:
        return _scim(exc.status, exc.body())
    except DBAPIError as exc:
        from boss_engine.identity.bindings import is_last_owner_violation

        if is_last_owner_violation(exc):
            # The change would leave the org with no active Owner (the deferred trigger refused
            # the commit): a SCIM conflict, never a 500.
            return _error(409, "this change would leave the organisation without an Owner",
                          "mutability")
        raise


async def _with_body(request: Request, work: Callable[[Any, AuthContext, dict], Any],
                     status: int = 200) -> Response:
    try:
        payload = await _body(request)
    except scim.ScimError as exc:
        return _scim(exc.status, exc.body())
    return await run_in_threadpool(_handle, request, lambda db, ctx: work(db, ctx, payload), status)


def build_identity_scim_router() -> APIRouter:
    router = APIRouter(tags=["identity-scim"])

    # Discovery (RFC 7644 §4): what this SCIM server supports. Behind the same SCIM key as every
    # other route (Okta's connector sends it on these calls too).
    @router.get("/scim/v2/ServiceProviderConfig")
    @permit(P.SCIM_PROVISION)
    def service_provider_config(request: Request):
        return _handle(request, lambda db, ctx: scim.service_provider_config())

    @router.get("/scim/v2/Schemas")
    @permit(P.SCIM_PROVISION)
    def scim_schemas(request: Request):
        return _handle(request, lambda db, ctx: scim.listed(scim.schemas()))

    @router.get("/scim/v2/ResourceTypes")
    @permit(P.SCIM_PROVISION)
    def scim_resource_types(request: Request):
        return _handle(request, lambda db, ctx: scim.listed(scim.resource_types()))

    @router.get("/scim/v2/Users")
    @permit(P.SCIM_PROVISION)
    def list_users(request: Request, filter: Optional[str] = None, startIndex: int = 1,  # noqa: A002,N803
                   count: int = 100):
        return _handle(request, lambda db, ctx: scim.list_users(db, ctx, filter_=filter, start=startIndex,
                                                                 count=count))

    @router.post("/scim/v2/Users")
    @permit(P.SCIM_PROVISION)
    async def create_user(request: Request):
        return await _with_body(request, scim.create_user, 201)

    @router.get("/scim/v2/Users/{user_id}")
    @permit(P.SCIM_PROVISION)
    def get_user(user_id: str, request: Request):
        return _handle(request, lambda db, ctx: scim.get_user(db, ctx, user_id))

    @router.put("/scim/v2/Users/{user_id}")
    @permit(P.SCIM_PROVISION)
    async def replace_user(user_id: str, request: Request):
        return await _with_body(request, lambda db, ctx, p: scim.replace_user(db, ctx, user_id, p))

    @router.patch("/scim/v2/Users/{user_id}")
    @permit(P.SCIM_PROVISION)
    async def patch_user(user_id: str, request: Request):
        return await _with_body(request, lambda db, ctx, p: scim.patch_user(db, ctx, user_id, p))

    @router.delete("/scim/v2/Users/{user_id}")
    @permit(P.SCIM_PROVISION)
    def delete_user(user_id: str, request: Request):
        return _handle(request, lambda db, ctx: scim.delete_user(db, ctx, user_id))

    @router.get("/scim/v2/Groups")
    @permit(P.SCIM_PROVISION)
    def list_groups(request: Request, filter: Optional[str] = None, startIndex: int = 1,  # noqa: A002,N803
                    count: int = 100):
        return _handle(request, lambda db, ctx: scim.list_groups(db, ctx, filter_=filter, start=startIndex,
                                                                  count=count))

    @router.post("/scim/v2/Groups")
    @permit(P.SCIM_PROVISION)
    async def create_group(request: Request):
        return await _with_body(request, scim.create_group, 201)

    @router.get("/scim/v2/Groups/{group_id}")
    @permit(P.SCIM_PROVISION)
    def get_group(group_id: str, request: Request):
        return _handle(request, lambda db, ctx: scim.get_group(db, ctx, group_id))

    @router.put("/scim/v2/Groups/{group_id}")
    @permit(P.SCIM_PROVISION)
    async def replace_group(group_id: str, request: Request):
        return await _with_body(request, lambda db, ctx, p: scim.replace_group(db, ctx, group_id, p))

    @router.patch("/scim/v2/Groups/{group_id}")
    @permit(P.SCIM_PROVISION)
    async def patch_group(group_id: str, request: Request):
        return await _with_body(request, lambda db, ctx, p: scim.patch_group(db, ctx, group_id, p))

    @router.delete("/scim/v2/Groups/{group_id}")
    @permit(P.SCIM_PROVISION)
    def delete_group(group_id: str, request: Request):
        return _handle(request, lambda db, ctx: scim.delete_group(db, ctx, group_id))

    return router
