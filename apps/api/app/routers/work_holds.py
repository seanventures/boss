"""Holding a whole work tree over HTTP (BOSSAPP-878; services/work_holds.py).

`POST /v1/work-holds` holds the tree under a work item (its subtree and every run of it) or a
workflow run (it and its child runs): mode `pause` pauses what is running now, `drain` lets it
finish and starts nothing new. `POST /v1/work-holds/{id}/restore` puts it back exactly. Both take
`dry_run` and answer with the plan, changing nothing. Holding and restoring control runs, so both
need workflow_run:control, attributed to the signed-in member.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit


class HoldBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    actor: Optional[str] = None
    root_kind: str = Field(pattern="^(work_item|workflow_run)$")
    root_id: str = Field(min_length=1, max_length=200)
    mode: str = Field(default="pause", pattern="^(pause|drain)$")
    reason: str = Field(default="", max_length=500)
    dry_run: bool = False


class RestoreBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    actor: Optional[str] = None
    dry_run: bool = False


def build_work_holds_router(*, engine, require_reader: Callable[..., None],
                            require_write_actor: Callable[..., str],
                            viewing: Optional[Callable[[Any], None]] = None) -> APIRouter:
    from boss_engine.services import work_holds as holds

    router = APIRouter(tags=["work-holds"])

    def _errors(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except holds.HoldConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (holds.HoldError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def _write(project_id: str, claimed: Optional[str], fn: Callable[[Any, str], Any]) -> Any:
        with begin(engine) as c:
            actor = require_write_actor(c, project_id, claimed, "workflow_execute")
            if viewing is not None:
                viewing(c)
            return _errors(lambda: fn(c, actor))

    @router.get("/v1/work-holds")
    @permit(P.WORKFLOW_RUN_READ, scope=Scope.project(query="project_id"))
    def list_holds(project_id: str, active_only: bool = True) -> dict:
        with begin(engine) as c:
            require_reader(c, project_id)
            return {"holds": holds.list_holds(c, project_id, active_only=active_only)}

    @router.get("/v1/work-holds/{hold_id}")
    @permit(P.WORKFLOW_RUN_READ, scope=Scope.project(query="project_id"))
    def get_hold(hold_id: str, project_id: str) -> dict:
        with begin(engine) as c:
            require_reader(c, project_id)
            return {"hold": _errors(lambda: holds.get_hold(c, project_id, hold_id))}

    @router.post("/v1/work-holds")
    @permit(P.WORKFLOW_RUN_CONTROL, scope=Scope.project(body="project_id"))
    def hold(payload: HoldBody) -> dict:
        return {"hold": _write(payload.project_id, payload.actor, lambda c, actor: holds.pause_tree(
            c, project_id=payload.project_id, root_kind=payload.root_kind,
            root_id=payload.root_id, actor=actor, mode=payload.mode, reason=payload.reason,
            dry_run=payload.dry_run))}

    @router.post("/v1/work-holds/{hold_id}/restore")
    @permit(P.WORKFLOW_RUN_CONTROL, scope=Scope.project(body="project_id"))
    def restore(hold_id: str, payload: RestoreBody) -> dict:
        return {"hold": _write(payload.project_id, payload.actor, lambda c, actor: holds.restore_tree(
            c, project_id=payload.project_id, hold_id=hold_id, actor=actor,
            dry_run=payload.dry_run))}

    return router
