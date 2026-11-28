"""Zero-token recurring jobs, change-only monitors (BOSSAPP-939), and learned skills (BOSSAPP-941)."""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit


class ZeroTokenJobCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    kind: str = Field(pattern="^(script|command|change_monitor)$")
    cadence: str = Field(default="interval", pattern="^(interval|hourly|daily|weekly)$")
    interval_minutes: int = Field(default=15, ge=1, le=10080)
    command_spec: dict[str, Any] = Field(default_factory=dict)
    watch_target: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    actor: Optional[str] = None


class ZeroTokenRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    actor: Optional[str] = None


class LearnedSkillsDetectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    min_occurrences: int = Field(default=3, ge=2, le=50)
    window_days: int = Field(default=30, ge=1, le=180)
    actor: Optional[str] = None


class LearnedSkillApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    actor: Optional[str] = None


def build_zero_token_and_learned_skills_router(
    *,
    engine,
    require_reader: Callable[..., None],
    require_write_actor: Callable[..., str],
) -> APIRouter:
    from boss_engine.services import learned_skills, zero_token_jobs

    router = APIRouter(tags=["zero-token-jobs", "learned-skills"])

    @router.get("/v1/zero-token-jobs")
    @permit(P.WORKFLOW_READ, scope=Scope.project(query="project_id"))
    def list_zero_token_jobs(project_id: str) -> dict:
        with begin(engine) as c:
            require_reader(c, project_id)
            return {"jobs": zero_token_jobs.list_jobs(c, project_id)}

    @router.post("/v1/zero-token-jobs", status_code=201)
    @permit(P.WORKFLOW_SCHEDULE_MANAGE, scope=Scope.project(body="project_id"))
    def create_zero_token_job(payload: ZeroTokenJobCreateBody) -> dict:
        with begin(engine) as c:
            actor = require_write_actor(c, payload.project_id, payload.actor, "workflow_manage")
            try:
                job = zero_token_jobs.create_job(
                    c,
                    payload.project_id,
                    name=payload.name,
                    kind=payload.kind,
                    cadence=payload.cadence,
                    interval_minutes=payload.interval_minutes,
                    command_spec=payload.command_spec,
                    watch_target=payload.watch_target,
                    enabled=payload.enabled,
                    created_by=actor,
                )
            except zero_token_jobs.ZeroTokenJobError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return {"job": job}

    @router.post("/v1/zero-token-jobs/{job_id}/run")
    @permit(P.WORKFLOW_SCHEDULE_MANAGE, scope=Scope.project(body="project_id"))
    def run_zero_token_job(job_id: str, payload: ZeroTokenRunBody) -> dict:
        with begin(engine) as c:
            actor = require_write_actor(c, payload.project_id, payload.actor, "workflow_manage")
            try:
                return zero_token_jobs.run_job(c, payload.project_id, job_id, actor=actor)
            except LookupError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except zero_token_jobs.ZeroTokenJobError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/v1/zero-token-jobs/{job_id}/runs")
    @permit(P.WORKFLOW_READ, scope=Scope.project(query="project_id"))
    def get_zero_token_job_runs(
        job_id: str, project_id: str, limit: int = Query(30, ge=1, le=200)
    ) -> dict:
        with begin(engine) as c:
            require_reader(c, project_id)
            try:
                return {"runs": zero_token_jobs.list_runs(c, project_id, job_id, limit=limit)}
            except LookupError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/v1/learned-skills/detect")
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.project(body="project_id"))
    def detect_learned_skills(payload: LearnedSkillsDetectBody) -> dict:
        with begin(engine) as c:
            actor = require_write_actor(c, payload.project_id, payload.actor, "decision_resolve")
            return learned_skills.draft_learned_skills(
                c,
                payload.project_id,
                min_occurrences=payload.min_occurrences,
                window_days=payload.window_days,
                actor=actor,
            )

    @router.get("/v1/learned-skills/pending")
    @permit(P.WORK_ITEM_READ, scope=Scope.project(query="project_id"))
    def get_pending_learned_skills(project_id: str) -> dict:
        with begin(engine) as c:
            require_reader(c, project_id)
            return {"pending": learned_skills.list_pending_learned_skills(c, project_id)}

    @router.post("/v1/learned-skills/{decision_id}/approve")
    @permit(P.DECISION_APPROVE, scope=Scope.of("decision", path="decision_id"))
    def approve_learned_skill(decision_id: str, payload: LearnedSkillApproveBody) -> dict:
        with begin(engine) as c:
            actor = require_write_actor(c, payload.project_id, payload.actor, "decision_resolve")
            try:
                return learned_skills.approve_and_publish_learned_skill(
                    c, payload.project_id, decision_id, actor=actor
                )
            except LookupError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc

    return router
