"""Starting a project, the simple way (BOSSAPP-810): two reads for the console's New project flow.

GET /v1/projects/{project_id}/start         the project page ("What should we add?"): its
                                            repository and how far its code is mapped, which
                                            workflow Start runs with its estimate, the example
                                            chips, the five "what happens next" tiles, and each
                                            piece of work with its state and one action.
GET /v1/github/repos/{repo_id}/facts        Import › "BOSS reads it": a registry repository's
                                            branch, language, framework, build, tests and what it
                                            runs as, and its mapping progress in this project.

Read-only. Every write the flow makes goes through a route that already exists: POST /v1/projects,
PUT /v1/projects/{id}/repos/{repo}, POST /v1/templates/{id}/apply and
POST /v1/delivery-workflows/start (services/start_project.py says which).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, permit

router = APIRouter(tags=["projects"])
_settings = get_settings()
_engine = get_engine(_settings)


@router.get("/v1/projects/{project_id}/start")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def project_start_view(project_id: str) -> dict[str, Any]:
    """The project page's one read (services/start_project.view)."""
    from boss_engine.services import start_project

    with begin(_engine) as connection:
        try:
            return start_project.view(connection, project_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from None


@router.get("/v1/github/repos/{repo_id}/facts")
@permit(P.REPOSITORY_READ, scope=Scope.project(query="project_id"))
def repo_facts(repo_id: str, project_id: str = Query(...)) -> Any:
    """What BOSS reads from one registry repository before (and after) it is imported."""
    from boss_engine.multirepo.common import MultiRepoError
    from boss_engine.services import start_project

    with begin(_engine) as connection:
        try:
            return start_project.repo_facts(connection, project_id, repo_id)
        except MultiRepoError as error:
            return JSONResponse(error.body(), status_code=error.status)
