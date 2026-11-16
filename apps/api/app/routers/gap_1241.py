"""HTTP for the live frame beside the chat (BOSSAPP-1241).

The body is the build. This router does not start one and does not call GitHub.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from boss_engine.identity import P, Scope, permit
from boss_engine.services.builder_live_frame import live_frame, publish
from boss_engine.services.builder_live_frame import reset_publishes as clear_publishes

router = APIRouter(prefix="/v1/builder/live-frame", tags=["builder-live-frame"])


def reset_publishes() -> None:
    clear_publishes()


class BuildBody(BaseModel):
    id: str = ""
    title: str = ""
    status: str = "building"
    app_url: str | None = None
    chat: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    github_connected: bool = False
    fake: bool = False
    device: str | None = None
    publish_to: str = "local"


def _payload(body: BuildBody) -> dict[str, Any]:
    return body.model_dump()


@router.post("")
@permit(P.PREVIEW_READ, scope=Scope.project(body="project_id"), readonly_post=True)
def show_frame(body: BuildBody) -> Any:
    try:
        return live_frame(_payload(body))
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "bad_build", "message": str(exc)},
        )


@router.post("/publish")
@permit(P.PREVIEW_MANAGE, scope=Scope.project(body="project_id"))
def publish_frame(body: BuildBody) -> Any:
    try:
        return publish(_payload(body), publish_to=body.publish_to)
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "bad_build", "message": str(exc)},
        )
