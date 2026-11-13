"""The work-item graph over HTTP (BOSSAPP-830/831): the tree, CRUD, relations, labels, goals,
the done door and a policy-aware start. Each route is one call into services/work_items.

WHO MAY DO WHAT: reading is work_item:read; every change is work_item:manage, attributed to the
signed-in member (require_write_actor with the member permission task_create) — never to a
claimed actor. Completing an item goes through the execution policy, so "done" here can answer
`in_review` with the stage decision it raised.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    actor: Optional[str] = None


class CreateBody(Body):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=8000)
    parent_id: Optional[str] = None
    goal_id: Optional[str] = None
    assignee: Optional[str] = None
    responsible_person: Optional[str] = None
    status: str = "todo"
    labels: list[str] = Field(default_factory=list, max_length=20)
    execution_policy: Optional[dict[str, Any]] = None


class UpdateBody(Body):
    title: Optional[str] = Field(default=None, max_length=300)
    description: Optional[str] = Field(default=None, max_length=8000)
    parent_id: Optional[str] = None
    goal_id: Optional[str] = None
    assignee: Optional[str] = None
    responsible_person: Optional[str] = None
    status: Optional[str] = None
    execution_policy: Optional[dict[str, Any]] = None
    due_at: Optional[str] = None


class RelationBody(Body):
    kind: str = Field(pattern="^(blocks|blocked_by|relates_to|duplicates)$")
    other_id: str = Field(min_length=1, max_length=200)


class LabelsBody(Body):
    names: list[str] = Field(min_length=1, max_length=20)


class LabelBody(Body):
    name: str = Field(min_length=1, max_length=40)


class GoalBody(Body):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=4000)
    parent_id: Optional[str] = None
    owner: Optional[str] = None
    target_date: Optional[str] = None


class StartBody(Body):
    workflow_version_id: str = Field(min_length=1, max_length=200)
    input: dict[str, Any] = Field(default_factory=dict)


def build_work_items_router(*, engine, settings: Callable[[], Any],
                            require_reader: Callable[..., None],
                            require_write_actor: Callable[..., str]) -> APIRouter:
    from boss_engine.services import work_items as wi
    from boss_engine.services.execution_policy import PolicyError

    router = APIRouter(tags=["work-items"])

    def _errors(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (wi.WorkItemError, PolicyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def _write(payload: Body, fn: Callable[[Any, str], Any]) -> Any:
        with begin(engine) as c:
            actor = require_write_actor(c, payload.project_id, payload.actor, "task_create")
            return _errors(lambda: fn(c, actor))

    # ── reading ──────────────────────────────────────────────────────────────────────────────
    @router.get("/v1/work-items/tree")
    @permit(P.WORK_ITEM_READ, scope=Scope.project(query="project_id"))
    def tree(project_id: str, include_closed: bool = True, include_ambient: bool = False,
             label: Optional[str] = None, limit: int = Query(500, ge=1, le=2000)) -> dict:
        with begin(engine) as c:
            require_reader(c, project_id)
            return _errors(lambda: wi.tree(c, project_id, include_closed=include_closed,
                                           include_ambient=include_ambient, label=label,
                                           limit=limit))

    @router.get("/v1/work-items/labels")
    @permit(P.WORK_ITEM_READ, scope=Scope.project(query="project_id"))
    def labels(project_id: str) -> dict:
        with begin(engine) as c:
            require_reader(c, project_id)
            return {"labels": wi.list_labels(c, project_id)}

    @router.get("/v1/work-items/goals")
    @permit(P.WORK_ITEM_READ, scope=Scope.project(query="project_id"))
    def goals(project_id: str) -> dict:
        with begin(engine) as c:
            require_reader(c, project_id)
            return {"goals": wi.list_goals(c, project_id)}

    @router.post("/v1/work-items/goals")
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.project(body="project_id"))
    def create_goal(payload: GoalBody) -> dict:
        return {"goal": _write(payload, lambda c, actor: wi.create_goal(
            c, project_id=payload.project_id, title=payload.title,
            description=payload.description, parent_id=payload.parent_id, owner=payload.owner,
            target_date=payload.target_date, actor=actor))}

    @router.get("/v1/work-items/{work_item_id}")
    @permit(P.WORK_ITEM_READ, scope=Scope.of("work_item", path="work_item_id"))
    def get_item(work_item_id: str, project_id: str) -> dict:
        with begin(engine) as c:
            require_reader(c, project_id)
            return {"item": _errors(lambda: wi.get_item(c, project_id, work_item_id))}

    # ── changing ─────────────────────────────────────────────────────────────────────────────
    @router.post("/v1/work-items", status_code=201)
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.project(body="project_id"))
    def create_item(payload: CreateBody) -> dict:
        return {"item": _write(payload, lambda c, actor: wi.create_item(
            c, settings(), project_id=payload.project_id, title=payload.title,
            description=payload.description, parent_id=payload.parent_id,
            goal_id=payload.goal_id, assignee=payload.assignee,
            responsible_person=payload.responsible_person, status=payload.status,
            labels=payload.labels, execution_policy=payload.execution_policy, actor=actor))}

    @router.patch("/v1/work-items/{work_item_id}")
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.of("work_item", path="work_item_id"))
    def update_item(work_item_id: str, payload: UpdateBody) -> dict:
        fields = payload.model_dump(exclude_unset=True, exclude={"project_id", "actor"})
        return {"item": _write(payload, lambda c, actor: wi.update_item(
            c, settings(), project_id=payload.project_id, item_id=work_item_id, fields=fields,
            actor=actor))}

    @router.delete("/v1/work-items/{work_item_id}")
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.of("work_item", path="work_item_id"))
    def delete_item(work_item_id: str, project_id: str, actor: Optional[str] = None) -> dict:
        with begin(engine) as c:
            who = require_write_actor(c, project_id, actor, "task_create")
            _errors(lambda: wi.delete_item(c, project_id=project_id, item_id=work_item_id,
                                           actor=who))
        return {"deleted": work_item_id}

    @router.post("/v1/work-items/{work_item_id}/complete")
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.of("work_item", path="work_item_id"))
    def complete(work_item_id: str, payload: Body) -> dict:
        def run(c, actor):
            verdict = wi.complete_item(c, settings(), project_id=payload.project_id,
                                       item_id=work_item_id, actor=actor,
                                       origin=f"person:{actor}")
            return {**verdict, "item": wi.get_item(c, payload.project_id, work_item_id)}

        return _write(payload, run)

    @router.post("/v1/work-items/{work_item_id}/start")
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.of("work_item", path="work_item_id"))
    def start(work_item_id: str, payload: StartBody) -> dict:
        return _write(payload, lambda c, actor: wi.defer_start(
            c, project_id=payload.project_id, item_id=work_item_id,
            workflow_version_id=payload.workflow_version_id, input_data=payload.input,
            actor=actor))

    @router.post("/v1/work-items/{work_item_id}/relations")
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.of("work_item", path="work_item_id"))
    def relate(work_item_id: str, payload: RelationBody) -> dict:
        return {"relations": _write(payload, lambda c, actor: wi.add_relation(
            c, project_id=payload.project_id, item_id=work_item_id, other_id=payload.other_id,
            kind=payload.kind, actor=actor))}

    @router.post("/v1/work-items/{work_item_id}/relations/remove")
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.of("work_item", path="work_item_id"))
    def unrelate(work_item_id: str, payload: RelationBody) -> dict:
        return {"relations": _write(payload, lambda c, actor: wi.remove_relation(
            c, settings(), project_id=payload.project_id, item_id=work_item_id,
            other_id=payload.other_id, kind=payload.kind, actor=actor))}

    @router.post("/v1/work-items/{work_item_id}/labels")
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.of("work_item", path="work_item_id"))
    def add_labels(work_item_id: str, payload: LabelsBody) -> dict:
        return {"labels": _write(payload, lambda c, actor: wi.add_labels(
            c, payload.project_id, work_item_id, payload.names, actor=actor))}

    @router.post("/v1/work-items/{work_item_id}/labels/remove")
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.of("work_item", path="work_item_id"))
    def remove_label(work_item_id: str, payload: LabelBody) -> dict:
        return {"labels": _write(payload, lambda c, actor: wi.remove_label(
            c, payload.project_id, work_item_id, payload.name, actor=actor))}

    return router
