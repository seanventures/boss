"""Project Manager steward HTTP — operate, test, validate, improve.

Thin routes over ``ProcessManagerService``. If the runtime module is missing
(sibling pm-runtime not merged yet), every endpoint returns an honest 503.
Never invents scores.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import get_settings
from ..db import begin, get_engine


router = APIRouter(tags=["process-manager"])

UNAVAILABLE = "Process manager runtime is unavailable."


class ProcessManagerActorRequest(BaseModel):
    project_id: str
    actor: str = "executive_console"


def _load_service_class() -> Optional[type]:
    try:
        from boss_engine.services.process_manager import ProcessManagerService
    except ImportError:
        return None
    return ProcessManagerService


def _service() -> Any:
    cls = _load_service_class()
    if cls is None:
        raise HTTPException(status_code=503, detail=UNAVAILABLE)
    return cls(get_settings())


def _require(connection, project_id: str, actor: str) -> None:
    from apps.api.app.main import require_project_actor

    require_project_actor(connection, project_id, actor, "agent_chat")


def _invoke(service: Any, names: tuple[str, ...], *args: Any, **kwargs: Any) -> Any:
    for name in names:
        fn = getattr(service, name, None)
        if callable(fn):
            return fn(*args, **kwargs)
    raise HTTPException(
        status_code=503,
        detail=f"{UNAVAILABLE} Missing {names[0]}.",
    )


def _require_project(project_id: str) -> str:
    cleaned = project_id.strip()
    if not cleaned:
        raise HTTPException(status_code=422, detail="project_id is required")
    return cleaned


@router.get("/v1/process-manager")
def get_process_manager(project_id: str) -> dict[str, Any]:
    project_id = _require_project(project_id)
    with begin(get_engine()) as connection:
        try:
            return _invoke(_service(), ("get", "snapshot", "status"), connection, project_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/v1/process-manager/ensure")
def ensure_process_manager(payload: ProcessManagerActorRequest) -> dict[str, Any]:
    project_id = _require_project(payload.project_id)
    with begin(get_engine()) as connection:
        _require(connection, project_id, payload.actor)
        try:
            return _invoke(
                _service(),
                ("ensure", "ensure_steward"),
                connection,
                project_id=project_id,
                actor=payload.actor,
            )
        except TypeError:
            return _invoke(
                _service(),
                ("ensure", "ensure_steward"),
                connection,
                project_id,
                payload.actor,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/v1/process-manager/tick")
def tick_process_manager(payload: ProcessManagerActorRequest) -> dict[str, Any]:
    project_id = _require_project(payload.project_id)
    with begin(get_engine()) as connection:
        _require(connection, project_id, payload.actor)
        try:
            return _invoke(
                _service(),
                ("tick",),
                connection,
                project_id=project_id,
                actor=payload.actor,
            )
        except TypeError:
            return _invoke(
                _service(),
                ("tick",),
                connection,
                project_id,
                payload.actor,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/v1/process-manager/metrics")
def get_process_manager_metrics(project_id: str) -> dict[str, Any]:
    project_id = _require_project(project_id)
    with begin(get_engine()) as connection:
        try:
            return _invoke(
                _service(),
                ("metrics", "get_metrics"),
                connection,
                project_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/v1/process-manager/proposals")
def get_process_manager_proposals(project_id: str) -> dict[str, Any]:
    project_id = _require_project(project_id)
    with begin(get_engine()) as connection:
        try:
            return _invoke(
                _service(),
                ("proposals", "list_proposals"),
                connection,
                project_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


