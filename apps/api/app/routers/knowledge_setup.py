"""A knowledge-graph project's guided setup (BOSSAPP-768 project flow).

GET /v1/knowledge/setup   where the setup is: each step (import code, assign the team, first
                          scan, review) with its state and real counts, the team and the agents
                          that can join it, the areas a first scan can read, and what is next.

One read for the Knowledge Graph area's stepper, the project home's lifecycle card and the
agent's `knowledge_project_setup` tool (services/knowledge/project_setup.py). The steps write
through the routes that already exist: PUT /v1/projects/{id}/repos/{repo} (import),
POST /v1/projects/{id}/agents action=set (team), the workflow start (scan) and Rules review.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, permit
from boss_engine.services.knowledge.service import READ

router = APIRouter(prefix="/v1/knowledge", tags=["knowledge"])
_settings = get_settings()
_engine = get_engine(_settings)


@router.get("/setup")
@permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
def knowledge_setup(project_id: str = Query(...), actor: str = Query(...)) -> dict[str, Any]:
    """The setup's state (services/knowledge/project_setup.status). Resumable by construction:
    every step is read from the records its own service writes."""
    from boss_engine.services.knowledge import project_setup

    from .knowledge import _actor, _http

    try:
        with begin(_engine) as connection:
            _actor(connection, project_id, actor, READ)
            return project_setup.status(connection, project_id)
    except Exception as error:  # noqa: BLE001 — mapped to HTTP
        raise _http(error) from error
