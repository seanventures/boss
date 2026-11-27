"""Quick Lane HTTP API (BOSSAPP-795..799, docs/product/QUICK_LANE.md).

Provides governed entry points for Console Quick fix, triage preview, run inspection,
CI-after-open webhooks/reports, and worker execution of ``quick.fix`` jobs.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.identity import P, Scope, permit
from boss_engine.services.quick_lane import (
    DEFAULT_COST_CAP_USD_MICROS,
    MAX_WALL_CLOCK_SECONDS,
    admit_quick_job,
    get_quick_run,
    handle_ci_result,
    lane_from_labels,
    list_quick_runs,
    start_quick_fix,
    triage_quick_lane,
)

from ..config import get_settings
from ..db import begin, get_engine

router = APIRouter(tags=["quick-lane"])


class TriageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    title: str = Field(default="", max_length=400)
    instructions: str = Field(default="", max_length=20000)
    kind: Optional[str] = Field(default=None, max_length=60)
    repro: Optional[str] = Field(default=None, max_length=8000)
    labels: list[str] = Field(default_factory=list)
    force_lane: Optional[str] = Field(default=None, max_length=20)
    diff_estimate: Optional[dict[str, Any]] = None


class StartQuickFixBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=400)
    instructions: str = Field(min_length=1, max_length=20000)
    actor: Optional[str] = Field(default=None, max_length=200)
    source: str = Field(default="console", max_length=40)
    requested_by_person: Optional[bool] = None
    task_id: Optional[str] = Field(default=None, max_length=200)
    repro: Optional[str] = Field(default=None, max_length=8000)
    labels: list[str] = Field(default_factory=list)
    touched_paths: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    study_record: Optional[dict[str, Any]] = None
    cost_cap_usd_micros: int = Field(default=DEFAULT_COST_CAP_USD_MICROS, ge=0)
    wall_clock_cap_seconds: float = Field(default=MAX_WALL_CLOCK_SECONDS, gt=0)
    dedupe_key: Optional[str] = Field(default=None, max_length=200)


class CiResultBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    ci_status: str = Field(min_length=1, max_length=40)
    ci_logs: str = Field(default="", max_length=20000)
    actor: Optional[str] = Field(default=None, max_length=200)


class WorkerStepBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    host_id: str = Field(min_length=1, max_length=200)
    job_id: Optional[str] = Field(default=None, max_length=200)
    lease_token: Optional[str] = Field(default=None, max_length=200)
    available_memory_mb: Optional[int] = Field(default=None, ge=0)


@router.post("/v1/quick-lane/triage")
@permit(P.WORKFLOW_RUN_READ, scope=Scope.project(body="project_id"))
def triage_endpoint(body: TriageBody) -> dict[str, Any]:
    spec = {
        "title": body.title,
        "instructions": body.instructions,
        "description": body.instructions,
        "kind": body.kind or "",
        "repro": body.repro,
        "labels": body.labels,
    }
    return triage_quick_lane(
        spec,
        diff_estimate=body.diff_estimate,
        force_lane=body.force_lane,
        labels=body.labels,
    )


@router.post("/v1/quick-lane/runs")
@permit(P.WORKFLOW_RUN_START, scope=Scope.project(body="project_id"))
def start_quick_fix_endpoint(body: StartQuickFixBody) -> dict[str, Any]:
    settings = get_settings()
    engine = get_engine(settings)
    actor = (body.actor or "").strip() or "operator"
    source = "ticket_label" if lane_from_labels(body.labels) == "quick" and body.source == "ticket_label" else body.source
    with begin(engine) as connection:
        return start_quick_fix(
            connection,
            project_id=body.project_id.strip(),
            title=body.title.strip(),
            instructions=body.instructions.strip(),
            actor=actor,
            source=source,
            requested_by_person=body.requested_by_person,
            task_id=body.task_id,
            spec_extra={
                "kind": "bug",
                "repro": body.repro,
                "labels": body.labels,
                "touched_paths": body.touched_paths,
                "symbols": body.symbols,
                "study_record": body.study_record,
            },
            cost_cap_usd_micros=body.cost_cap_usd_micros,
            wall_clock_cap_seconds=body.wall_clock_cap_seconds,
            dedupe_key=body.dedupe_key,
        )


@router.get("/v1/quick-lane/runs")
@permit(P.WORKFLOW_RUN_READ, scope=Scope.project(query="project_id"))
def list_quick_runs_endpoint(
    project_id: str = Query(..., min_length=1),
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    settings = get_settings()
    engine = get_engine(settings)
    with begin(engine) as connection:
        items = list_quick_runs(connection, project_id=project_id, status=status, limit=limit)
    return {"items": items, "total": len(items)}


@router.get("/v1/quick-lane/runs/{run_id}")
@permit(P.WORKFLOW_RUN_READ, scope=Scope.project(query="project_id"))
def get_quick_run_endpoint(
    run_id: str,
    project_id: str = Query(..., min_length=1),
) -> dict[str, Any]:
    settings = get_settings()
    engine = get_engine(settings)
    with begin(engine) as connection:
        run = get_quick_run(connection, project_id=project_id, run_id=run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Quick-lane run {run_id} not found.")
    return run


@router.post("/v1/quick-lane/runs/{run_id}/ci-result")
@permit(P.WORKFLOW_RUN_CONTROL, scope=Scope.project(body="project_id"))
def report_ci_result_endpoint(run_id: str, body: CiResultBody) -> dict[str, Any]:
    settings = get_settings()
    engine = get_engine(settings)
    actor = (body.actor or "").strip() or "ci_webhook"
    with begin(engine) as connection:
        try:
            return handle_ci_result(
                connection,
                project_id=body.project_id.strip(),
                run_id=run_id,
                ci_status=body.ci_status,
                ci_logs=body.ci_logs,
                actor=actor,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/v1/quick-lane/step")
@permit(P.WORKER_LEASE, scope=Scope.project(body="project_id"))
def run_quick_fix_step_endpoint(body: WorkerStepBody) -> dict[str, Any]:
    """Worker admission check for a leased ``quick.fix`` job."""
    settings = get_settings()
    engine = get_engine(settings)
    with begin(engine) as connection:
        admit = admit_quick_job(
            connection,
            run_id=body.run_id,
            project_id=body.project_id,
            host_id=body.host_id,
            available_memory_mb=body.available_memory_mb,
        )
        if not admit.admitted:
            return {
                "run_id": body.run_id,
                "status": "waiting_memory" if admit.reason == "insufficient_host_memory" else "queued",
                "job_deferred": True,
                "reason": admit.reason,
                "detail": admit.detail,
            }
        run = get_quick_run(connection, project_id=body.project_id, run_id=body.run_id)
        return {
            "run_id": body.run_id,
            "status": run["status"] if run else "running",
            "host_id": body.host_id,
            "admitted": True,
        }
