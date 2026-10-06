"""GET /v1/identity/me and GET /v1/identity/me/permissions: the console's identity contract.

Shapes are frozen in boss_engine/identity/contracts.py (TS mirror: apps/web/lib/identity/contract.ts).
Phase 0 ships the routes, their declarations and their response models; the bodies answer 501
until stream S1 (me) and S2 (effective permissions, called from here) fill them in.

The identity routers are ALWAYS enforced (router key `identity*`), so until authentication exists
these answer 503 `identity_unavailable` before reaching the handler. That is fail-closed by design.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from boss_engine.identity import authenticated
from boss_engine.identity.contracts import MePermissionsResponse, MeResponse

NOT_YET = {"error": "not_implemented", "detail": "identity Phase 0: this route is a frozen stub"}


def build_identity_me_router() -> APIRouter:
    router = APIRouter(tags=["identity"])

    @router.get("/v1/identity/me", response_model=MeResponse)
    @authenticated(self_only=True)
    def get_me():
        # S1: build MeResponse from the request's AuthContext (never from a body or query field).
        return JSONResponse(status_code=501, content=NOT_YET)

    @router.get("/v1/identity/me/permissions", response_model=MePermissionsResponse)
    @authenticated(self_only=True)
    def get_my_permissions(project_id: Optional[str] = None):
        # S1 wires it; S2's authz.effective_permissions(ctx, scope) computes it. project_id names
        # the scope to evaluate; an invisible project answers 404, never an empty list.
        return JSONResponse(status_code=501, content=NOT_YET)

    return router
