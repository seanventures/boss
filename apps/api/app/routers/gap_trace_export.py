"""Company endpoint for Codex and Claude Code trace export. The reconciler mounts `router`."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, permit
from boss_engine.services import trace_export

router = APIRouter(prefix="/v1/trace-export", tags=["trace-export"])


class DestinationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["braintrust", "otlp"]
    endpoint: str = Field(min_length=1, max_length=2000)


@router.put("/destination")
@permit(P.TELEMETRY_SHARE, scope=Scope.project(query="project_id"))
def put_destination(project_id: str, body: DestinationIn):
    engine = get_engine()
    try:
        with begin(engine) as connection:
            org_id = trace_export.org_for_project(connection, project_id)
            return trace_export.set_destination(
                connection, org_id=org_id, kind=body.kind, endpoint=body.endpoint)
    except LookupError as exc:
        raise HTTPException(404, "No project with that id.") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/destination")
@permit(P.TELEMETRY_READ, scope=Scope.project(query="project_id"))
def get_destination(project_id: str):
    engine = get_engine()
    try:
        with begin(engine) as connection:
            org_id = trace_export.org_for_project(connection, project_id)
            chosen = trace_export.destination(connection, org_id)
    except LookupError as exc:
        raise HTTPException(404, "No project with that id.") from exc
    return {"destination": chosen}


@router.delete("/destination")
@permit(P.TELEMETRY_SHARE, scope=Scope.project(query="project_id"))
def delete_destination(project_id: str):
    engine = get_engine()
    try:
        with begin(engine) as connection:
            org_id = trace_export.org_for_project(connection, project_id)
            connection.execute(
                text("DELETE FROM trace_export_destinations WHERE org_id = :org"), {"org": org_id})
    except LookupError as exc:
        raise HTTPException(404, "No project with that id.") from exc
    return {"destination": None}


@router.post("/runs/{run_id}")
@permit(P.TELEMETRY_SHARE, scope=Scope.project(query="project_id"))
def export_run(run_id: str, project_id: str):
    try:
        return trace_export.export_saved_trace(get_engine(), project_id=project_id, run_id=run_id)
    except LookupError as exc:
        raise HTTPException(404, "No project with that id.") from exc


@router.get("/failures")
@permit(P.TELEMETRY_READ, scope=Scope.project(query="project_id"))
def list_failures(project_id: str):
    return {"failures": trace_export.failed_exports(get_engine(), project_id=project_id)}
