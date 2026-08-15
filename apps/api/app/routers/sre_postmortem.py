"""HTTP surface for evidence-only AI SRE incident postmortems.

POST drafts/persists from existing evidence. GET returns the durable artifact.
Missing incidents 404. Never invents RCA. Does not resolve the incident. Not GA.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from boss_engine.services.reliability import ReliabilityService
from boss_engine.services.sre_postmortem import draft_postmortem, get_postmortem

from ..db import begin, get_engine

router = APIRouter(tags=["reliability"])


class PostmortemRequest(BaseModel):
    project_id: str
    actor: str = "executive_console"


def _require_actor(connection, project_id: str, actor: str) -> None:
    from apps.api.app.main import require_project_actor

    require_project_actor(connection, project_id, actor, "providerops_manage")


@router.post("/v1/reliability/incidents/{incident_id}/postmortem")
def create_incident_postmortem(incident_id: str, payload: PostmortemRequest) -> dict[str, Any]:
    with begin(get_engine()) as connection:
        _require_actor(connection, payload.project_id, payload.actor)
        result = draft_postmortem(connection, payload.project_id, incident_id)
        if result is None:
            raise HTTPException(status_code=404, detail="incident not found")
        return result


@router.get("/v1/reliability/incidents/{incident_id}/postmortem")
def read_incident_postmortem(
    incident_id: str,
    project_id: str = Query(...),
) -> dict[str, Any]:
    with begin(get_engine()) as connection:
        incident = ReliabilityService().get_incident(connection, project_id, incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="incident not found")
        result = get_postmortem(connection, project_id, incident_id)
        if result is None:
            raise HTTPException(status_code=404, detail="postmortem not found")
        return result
