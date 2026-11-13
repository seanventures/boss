"""Project toolchain images API (BOSSAPP-825).

One simple flow for Project › Settings › Toolchain:

  POST /v1/toolchain-images                      start a setup session
  POST /v1/toolchain-images/{id}/exec            one terminal command (audited)
  POST /v1/toolchain-images/{id}/capture         commit [+ push]; answers at once,
                                                 the chip turns ready/failed after
  POST /v1/toolchain-images/{id}/discard         close the session without an image
  GET  /v1/toolchain-images?project_id=&actor=   the project's images + which one builds use

Every route refuses, in words (422), when BOSS_SANDBOX_COMMAND_RUNTIME is not
docker — the only runtime that would ever use the image.
"""
from __future__ import annotations

from boss_engine.identity import P, Scope, permit  # noqa: E402  (identity S2)
from ..identity_guard import writing_get  # noqa: E402  (identity int-b)
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel, Field

from boss_engine.services import toolchain_images as tci

from ..config import get_settings
from ..db import begin, get_engine

router = APIRouter(prefix="/v1/toolchain-images", tags=["toolchain-images"])


class StartRequest(BaseModel):
    project_id: str
    actor: str
    base: Optional[str] = None


class ExecRequest(BaseModel):
    project_id: str
    actor: str
    command: str = Field(..., min_length=1, max_length=4000)


class ActorRequest(BaseModel):
    project_id: str
    actor: str


def _require_operator(connection: Any, project_id: str, actor: str,
                      *, mutating: bool = False) -> None:
    # The environments router's binding: the claimed actor is bound to the transport
    # principal and the project; a mutation (a root shell in a container) needs more.
    from apps.api.app.main import require_project_actor

    require_project_actor(connection, project_id, actor, "providerops_manage")
    if mutating:
        require_project_actor(connection, project_id, actor, "workflow_execute")


def _own(connection, image_id: str, project_id: str) -> dict:
    try:
        row = tci.get_image(connection, image_id)
    except tci.ToolchainImageNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if row.get("project_id") != project_id:
        raise HTTPException(status_code=404, detail=f"no toolchain image {image_id!r}.")
    return row


def _refusal(error: tci.ToolchainImageError) -> HTTPException:
    return HTTPException(status_code=422 if isinstance(error, tci.DockerRuntimeOff) else 409,
                         detail=str(error))


@writing_get(router, "")
@writing_get(router, "/")
@permit(P.HOST_READ, scope=Scope.project(query="project_id"), writes_on_get=True)
def list_images(project_id: str = Query(...), actor: str = Query(...)) -> dict[str, Any]:
    settings = get_settings()
    with begin(get_engine()) as connection:
        _require_operator(connection, project_id, actor)
        rows = tci.list_images(connection, project_id)
        in_use = tci.image_for_project(connection, project_id)
    return {
        "project_id": project_id,
        "docker_runtime": tci.docker_runtime_on(settings),
        "registry": tci.registry(settings) or None,
        "default_image": getattr(settings, "sandbox_container_image", None),
        "default_base": (getattr(settings, "toolchain_default_base", "")
                         or getattr(settings, "sandbox_container_image", None)),
        # What a docker-runtime build of this project runs right now.
        "builds_use": in_use or getattr(settings, "sandbox_container_image", None),
        "items": [tci.public_view(r, settings) for r in rows],
    }


@router.post("")
@router.post("/")
@permit(P.HOST_MANAGE, scope=Scope.project(body="project_id"))
def start_session(payload: StartRequest) -> dict[str, Any]:
    settings = get_settings()
    try:
        with begin(get_engine()) as connection:
            _require_operator(connection, payload.project_id, payload.actor, mutating=True)
            row = tci.start_session(connection, settings, project_id=payload.project_id,
                                    actor=payload.actor, base=payload.base)
            return tci.public_view(row, settings)
    except tci.ToolchainImageError as error:
        raise _refusal(error) from error


@router.post("/{image_id}/exec")
@permit(P.HOST_MANAGE, scope=Scope.project(body="project_id"))
def exec_command(image_id: str, payload: ExecRequest) -> dict[str, Any]:
    settings = get_settings()
    try:
        with begin(get_engine()) as connection:
            _require_operator(connection, payload.project_id, payload.actor, mutating=True)
            _own(connection, image_id, payload.project_id)
            return tci.exec_in_session(connection, settings, image_id, payload.command,
                                       actor=payload.actor)
    except tci.ToolchainImageError as error:
        raise _refusal(error) from error


def _finish(image_id: str, actor: str) -> None:
    with begin(get_engine()) as connection:
        tci.finish_capture(connection, get_settings(), image_id, actor=actor)


@router.post("/{image_id}/capture")
@permit(P.HOST_MANAGE, scope=Scope.project(body="project_id"))
def capture(image_id: str, payload: ActorRequest, background: BackgroundTasks) -> dict[str, Any]:
    settings = get_settings()
    try:
        with begin(get_engine()) as connection:
            _require_operator(connection, payload.project_id, payload.actor, mutating=True)
            _own(connection, image_id, payload.project_id)
            row = tci.begin_capture(connection, settings, image_id, actor=payload.actor)
    except tci.ToolchainImageError as error:
        raise _refusal(error) from error
    # Commit + push can take minutes; the console polls the list for the chip.
    background.add_task(_finish, image_id, payload.actor)
    return tci.public_view(row, settings)


@router.post("/{image_id}/discard")
@permit(P.HOST_MANAGE, scope=Scope.project(body="project_id"))
def discard(image_id: str, payload: ActorRequest) -> dict[str, Any]:
    settings = get_settings()
    try:
        with begin(get_engine()) as connection:
            _require_operator(connection, payload.project_id, payload.actor, mutating=True)
            _own(connection, image_id, payload.project_id)
            return tci.public_view(tci.discard(connection, settings, image_id,
                                               actor=payload.actor), settings)
    except tci.ToolchainImageError as error:
        raise _refusal(error) from error
