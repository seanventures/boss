"""Agent process HTTP — templates, start, advance, handoff timeline."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from boss_engine.services.agent_process import AgentProcessService

from ..config import get_settings
from ..db import begin, get_engine


router = APIRouter(tags=["agent-processes"])


class TemplateCreateRequest(BaseModel):
    project_id: str
    actor: str = "executive_console"
    process_key: str
    name: str
    description: str = ""
    spec: dict[str, Any]


class ProcessStartRequest(BaseModel):
    project_id: str
    actor: str = "executive_console"
    prd: str
    title: str = ""
    process_key: str = "prd_to_accepted_work"
    simulated: bool = Field(default=False)
    auto_build: bool = Field(
        default=False,
        description="If true, captain build enqueues a governed code.task.",
    )


class ProcessActorRequest(BaseModel):
    project_id: str
    actor: str = "executive_console"


def _service() -> AgentProcessService:
    return AgentProcessService(get_settings())


def _require(connection, project_id: str, actor: str) -> None:
    from apps.api.app.main import require_project_actor

    require_project_actor(connection, project_id, actor, "agent_chat")


@router.get("/v1/agent-processes/templates")
def list_templates(project_id: str) -> dict[str, Any]:
    with begin(get_engine()) as connection:
        return {
            "project_id": project_id,
            "templates": _service().list_templates(connection, project_id),
        }


@router.post("/v1/agent-processes/templates", status_code=201)
def create_template(payload: TemplateCreateRequest) -> dict[str, Any]:
    with begin(get_engine()) as connection:
        _require(connection, payload.project_id, payload.actor)
        try:
            return _service().create_template(
                connection,
                project_id=payload.project_id,
                actor=payload.actor,
                process_key=payload.process_key,
                name=payload.name,
                description=payload.description,
                spec=payload.spec,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/v1/agent-processes/runs")
def list_runs(project_id: str) -> dict[str, Any]:
    with begin(get_engine()) as connection:
        return {
            "project_id": project_id,
            "runs": _service().list_runs(connection, project_id),
        }


@router.post("/v1/agent-processes/runs", status_code=201)
def start_run(payload: ProcessStartRequest) -> dict[str, Any]:
    if not payload.project_id.strip() or not payload.prd.strip():
        raise HTTPException(status_code=422, detail="project_id and prd are required")
    with begin(get_engine()) as connection:
        _require(connection, payload.project_id, payload.actor)
        try:
            return _service().start_run(
                connection,
                project_id=payload.project_id,
                actor=payload.actor,
                prd=payload.prd,
                title=payload.title,
                process_key=payload.process_key,
                simulated=payload.simulated,
                auto_build=payload.auto_build,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/v1/agent-processes/runs/{run_id}")
def get_run(run_id: str, project_id: str) -> dict[str, Any]:
    with begin(get_engine()) as connection:
        try:
            return _service().get_run(connection, project_id, run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/v1/agent-processes/runs/{run_id}/advance")
def advance_run(run_id: str, payload: ProcessActorRequest) -> dict[str, Any]:
    with begin(get_engine()) as connection:
        _require(connection, payload.project_id, payload.actor)
        try:
            return _service().advance(
                connection,
                project_id=payload.project_id,
                actor=payload.actor,
                run_id=run_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/v1/agent-processes/tick-all")
def tick_all(payload: ProcessActorRequest) -> dict[str, Any]:
    with begin(get_engine()) as connection:
        _require(connection, payload.project_id, payload.actor)
        return _service().tick_due(
            connection, project_id=payload.project_id, actor=payload.actor
        )
