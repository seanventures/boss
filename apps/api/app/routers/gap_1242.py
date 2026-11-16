"""HTTP seam for marking one piece of a preview (BOSSAPP-1242).

Not mounted here. The reconciler includes ``router`` from the slice mount file.
"""
from __future__ import annotations

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from boss_engine.services.element_select import (
    ChangeRequest,
    ElementSelectError,
    ElementSelectService,
)

router = APIRouter(prefix="/v1/element-select", tags=["element-select"])

default_service = ElementSelectService()

_STATUS = {
    "invalid": 400,
    "not_found": 404,
    "budget_denied": 409,
    "effect_failed": 503,
}


class ApplyBody(BaseModel):
    org_id: str
    workspace_id: str = "local"
    project_id: str
    preview_id: str
    element_id: str
    action: str
    change: str
    actor: str = "founder"


def _raise(error: ElementSelectError) -> None:
    status = _STATUS.get(error.code, 400)
    raise HTTPException(status_code=status, detail=error.as_detail()) from error


@router.get("/preview/{preview_id}")
@permit(P.PREVIEW_READ, scope=Scope.project(query="project_id"))
def read_preview(preview_id: str, project_id: str) -> dict:
    try:
        return default_service.sheet(preview_id)
    except ElementSelectError as error:
        _raise(error)
    raise AssertionError("sheet returns or raises")


@router.post("/apply")
@permit(P.PREVIEW_MANAGE, scope=Scope.project(body="project_id"))
def apply_mark(body: ApplyBody) -> dict:
    request = ChangeRequest(
        org_id=body.org_id,
        workspace_id=body.workspace_id,
        project_id=body.project_id,
        preview_id=body.preview_id,
        element_id=body.element_id,
        action=body.action,
        change=body.change,
        actor=body.actor,
    )
    try:
        return default_service.apply(request)
    except ElementSelectError as error:
        _raise(error)
    raise AssertionError("apply returns or raises")
