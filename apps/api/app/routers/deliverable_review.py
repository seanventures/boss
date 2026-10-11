"""Human usefulness reviews. Recording one cannot start a job or authorize a provider effect."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.db import begin
from boss_engine.identity import P, Scope, lineage, permit
from boss_engine.identity.principal import PrincipalKind, PrincipalStatus
from boss_engine.services import deliverable_review


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: Literal["accepted", "changes_requested"]
    note: str = Field(default="", max_length=4000)
    submission_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,80}$")


def human_actor() -> str:
    ctx = lineage.current_context()
    acting = lineage.current()
    if (ctx is None or ctx.principal.kind != PrincipalKind.USER
            or ctx.principal.status != PrincipalStatus.ACTIVE or ctx.run_token is not None
            or (acting is not None and (acting.run_id or acting.chain or acting.system))):
        raise HTTPException(403, "Sign in as a person to review whether this result is useful.")
    return ctx.principal.id


def build_deliverable_review_router(*, engine, require_reader, require_write_actor) -> APIRouter:
    router = APIRouter(tags=["code-tasks"])

    @router.get("/v1/code-tasks/{run_id}/result-review")
    @permit(P.CODE_TASK_READ, scope=Scope.of("code_task", path="run_id"))
    def read_review(run_id: str):
        with begin(engine) as connection:
            project_id = require_reader(connection, run_id)
            result = deliverable_review.snapshot(connection, project_id, run_id)
            result["history"] = deliverable_review.history(connection, project_id, run_id)
            return result

    @router.post("/v1/code-tasks/{run_id}/result-review")
    @permit(P.CODE_TASK_STEER, scope=Scope.of("code_task", path="run_id"))
    def submit_review(run_id: str, payload: ReviewBody):
        actor = human_actor()
        with begin(engine) as connection:
            project_id = require_reader(connection, run_id)
            require_write_actor(connection, project_id, actor, "task_create")
            try:
                return deliverable_review.record(connection, project_id=project_id, run_id=run_id,
                                                 actor=actor, **payload.model_dump())
            except deliverable_review.ReviewConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
    return router
