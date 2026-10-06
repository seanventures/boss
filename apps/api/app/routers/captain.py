"""Captain coordination HTTP — roster, staffing plan, coordinate."""
from __future__ import annotations

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
from ..identity_guard import writing_get  # noqa: E402  (identity int-b)
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from boss_engine.services.captain_coordination import roster

from .. import request_bodies as open_bodies
from ..config import get_settings
from ..db import begin, get_engine


router = APIRouter(tags=["captain"])


class CoordinateRequest(BaseModel):
    project_id: str
    actor: str = "engineering_captain"


def _service():
    from boss_engine.services.captain_coordination import CaptainCoordinationService

    return CaptainCoordinationService(get_settings())


def _require_captain_actor(
    connection, project_id: str, claimed_actor: str, permission: str,
) -> str:
    """Bind a captain write to the caller's active project membership."""
    from apps.api.app.main import current_api_principal, require_project_actor

    actor = current_api_principal.get() or claimed_actor
    require_project_actor(connection, project_id, actor, permission)
    return actor


@router.get("/v1/captain/roster")
@permit(P.AUTOPILOT_READ, scope=Scope.org())
def captain_roster() -> dict[str, Any]:
    """Catalog of agents the captain may staff. Not a scorecard."""
    return {"agents": roster()}


@router.get("/v1/captain/plan")
@permit(P.AUTOPILOT_READ, scope=Scope.project(query="project_id"))
def captain_plan(project_id: str, run_id: str) -> dict[str, Any]:
    if not project_id.strip() or not run_id.strip():
        raise HTTPException(status_code=422, detail="project_id and run_id are required")
    with begin(get_engine()) as connection:
        return _service().plan_for_run(connection, project_id, run_id)


@router.post("/v1/captain/coordinate")
@permit(P.AUTOPILOT_OPERATE, scope=Scope.project(body="project_id"))
def captain_coordinate(payload: CoordinateRequest) -> dict[str, Any]:
    """Staff in-flight work, persist now-assignments, convene panels as needed."""
    if not payload.project_id.strip():
        raise HTTPException(status_code=422, detail="project_id is required")
    with begin(get_engine()) as connection:
        actor = _require_captain_actor(
            connection, payload.project_id, payload.actor, "workflow_execute",
        )
        return _service().coordinate(
            connection, payload.project_id, actor=actor,
        )


class PlaybookSaveRequest(BaseModel):
    project_id: str
    actor: str = "executive_console"
    playbook_key: str = ""
    name: str
    description: str = ""
    instructions: str = ""
    process_key: str = "prd_to_accepted_work"
    from_text: str = ""
    goal: dict[str, Any] | None = None
    constraints: dict[str, Any] | None = None
    loop: dict[str, Any] | None = None


class PlaybookStartRequest(BaseModel):
    project_id: str
    actor: str = "executive_console"
    prd: str
    attach_run_id: str | None = None
    auto_build: bool = False


class PlaybookLoopRequest(BaseModel):
    project_id: str
    actor: str = "engineering_captain"
    playbook_key: str | None = None


def _playbooks():
    from boss_engine.services.captain_playbook import CaptainPlaybookService

    return CaptainPlaybookService(get_settings())


@writing_get(router, "/v1/captain/playbooks")
@permit(P.AUTOPILOT_READ, scope=Scope.project(query="project_id"), writes_on_get=True)
def list_playbooks(project_id: str) -> dict[str, Any]:
    if not project_id.strip():
        raise HTTPException(status_code=422, detail="project_id is required")
    with begin(get_engine()) as connection:
        items = _playbooks().list(connection, project_id)
        return {"project_id": project_id, "playbooks": items}


@router.post("/v1/captain/playbooks/compile")
@permit(P.AUTOPILOT_CONFIGURE, scope=Scope.org())
def compile_playbook_endpoint(payload: open_bodies.CaptainPlaybookCompileBody) -> dict[str, Any]:
    payload = payload.payload()
    from boss_engine.services.captain_playbook import compile_playbook

    text = str((payload or {}).get("text") or (payload or {}).get("instructions") or "")
    try:
        return compile_playbook(text, name=(payload or {}).get("name"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/v1/captain/playbooks", status_code=201)
@permit(P.AUTOPILOT_CONFIGURE, scope=Scope.project(body="project_id"))
def save_playbook(payload: PlaybookSaveRequest) -> dict[str, Any]:
    from boss_engine.services.captain_playbook import compile_playbook

    if not payload.project_id.strip():
        raise HTTPException(status_code=422, detail="project_id is required")
    body = {
        "playbook_key": payload.playbook_key,
        "name": payload.name,
        "description": payload.description,
        "instructions": payload.instructions,
        "process_key": payload.process_key,
        "goal": payload.goal,
        "constraints": payload.constraints,
        "loop": payload.loop,
    }
    if payload.from_text.strip():
        compiled = compile_playbook(payload.from_text, name=payload.name)
        compiled.update({k: v for k, v in body.items() if v})
        body = compiled
    if not body.get("playbook_key"):
        raise HTTPException(status_code=422, detail="playbook_key or from_text is required")
    with begin(get_engine()) as connection:
        actor = _require_captain_actor(
            connection, payload.project_id, payload.actor, "workflow_manage",
        )
        return _playbooks().save(
            connection, project_id=payload.project_id, actor=actor, **body,
        )


@router.post("/v1/captain/playbooks/{playbook_key}/start", status_code=201)
@permit(P.AUTOPILOT_OPERATE, scope=Scope.project(body="project_id"))
def start_playbook(playbook_key: str, payload: PlaybookStartRequest) -> dict[str, Any]:
    if not payload.project_id.strip() or not payload.prd.strip():
        raise HTTPException(status_code=422, detail="project_id and prd are required")
    with begin(get_engine()) as connection:
        actor = _require_captain_actor(
            connection, payload.project_id, payload.actor, "workflow_execute",
        )
        if payload.auto_build:
            _require_captain_actor(
                connection, payload.project_id, actor, "task_create",
            )
        try:
            return _playbooks().start(
                connection,
                project_id=payload.project_id,
                actor=actor,
                playbook_key=playbook_key,
                prd=payload.prd,
                attach_run_id=payload.attach_run_id,
                auto_build=payload.auto_build,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/v1/captain/playbooks/{playbook_key}/loop")
@permit(P.AUTOPILOT_OPERATE, scope=Scope.project(body="project_id"))
def loop_playbook(playbook_key: str, payload: PlaybookLoopRequest) -> dict[str, Any]:
    if not payload.project_id.strip():
        raise HTTPException(status_code=422, detail="project_id is required")
    with begin(get_engine()) as connection:
        actor = _require_captain_actor(
            connection, payload.project_id, payload.actor, "workflow_execute",
        )
        return _playbooks().loop_once(
            connection,
            project_id=payload.project_id,
            actor=actor,
            playbook_key=playbook_key,
        )
