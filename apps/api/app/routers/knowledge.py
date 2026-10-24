"""Knowledge graph HTTP surface (BOSSAPP-622).

GET  /v1/knowledge/search
GET  /v1/knowledge/nodes/{node_id}
GET  /v1/knowledge/blast-radius
GET  /v1/knowledge/graph-status   (/v1/knowledge/status is BOSS-356's pack-context probe)
POST /v1/knowledge/facts/{fact_id}/promote
POST /v1/knowledge/facts/{fact_id}/reject
POST /v1/knowledge/facts/{fact_id}/forget
POST /v1/knowledge/ingest/tick     (the worker: drain the finish queue, sweep on request)
POST /v1/knowledge/cypher          (read-only Cypher over the project's AGE projection)
GET  /v1/knowledge/repositories    (each registered repository's code graph: state and counts)
POST /v1/knowledge/repositories/index          (index now: queue the repositories' index jobs)
POST /v1/knowledge/repositories/index/execute  (the worker: run one leased index job)

Manage calls require knowledge:manage and step_up=confirmed. That confirmation
is explicit in the body. It is not a WebAuthn ceremony.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, permit
from boss_engine.services.knowledge.service import (
    MANAGE,
    READ,
    KnowledgeService,
    require_confirmation,
)


router = APIRouter(prefix="/v1/knowledge", tags=["knowledge"])
_settings = get_settings()
_engine = get_engine(_settings)
_service = KnowledgeService()


def _actor(connection, project_id: str, actor: str, permission: str) -> None:
    from apps.api.app.main import require_project_actor

    require_project_actor(connection, project_id, actor, permission)


class ManageBody(BaseModel):
    project_id: str
    actor: str
    step_up: Optional[str] = None


def _http(error: Exception) -> HTTPException:
    from boss_engine.services.projects_archive import ProjectArchived

    if isinstance(error, ProjectArchived):      # index now, in an archived project
        return HTTPException(status_code=409, detail=error.detail())
    if isinstance(error, PermissionError):
        return HTTPException(status_code=403, detail=str(error))
    if isinstance(error, LookupError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, ValueError):
        return HTTPException(status_code=422, detail=str(error))
    return HTTPException(status_code=500, detail=error.__class__.__name__)


@router.get("/search")
@permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
def search(
    project_id: str = Query(...),
    actor: str = Query(...),
    q: str = Query(...),
) -> dict[str, Any]:
    try:
        with begin(_engine) as connection:
            _actor(connection, project_id, actor, READ)
            return _service.search(connection, project_id=project_id, query=q)
    except Exception as error:  # noqa: BLE001 — mapped to HTTP below
        raise _http(error) from error


@router.get("/nodes/{node_id}")
@permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
def node(
    node_id: str,
    project_id: str = Query(...),
    actor: str = Query(...),
) -> dict[str, Any]:
    try:
        with begin(_engine) as connection:
            _actor(connection, project_id, actor, READ)
            return _service.why(connection, project_id=project_id, node_id=node_id)
    except Exception as error:  # noqa: BLE001
        raise _http(error) from error


@router.get("/blast-radius")
@permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
def blast(
    project_id: str = Query(...),
    actor: str = Query(...),
    node_id: str = Query(...),
) -> dict[str, Any]:
    try:
        with begin(_engine) as connection:
            _actor(connection, project_id, actor, READ)
            return _service.blast(connection, project_id=project_id, node_id=node_id)
    except Exception as error:  # noqa: BLE001
        raise _http(error) from error


@router.get("/graph-status")
@permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
def status(project_id: str = Query(...), actor: str = Query(...)) -> dict[str, Any]:
    try:
        with begin(_engine) as connection:
            _actor(connection, project_id, actor, READ)
            return _service.status(connection, project_id=project_id)
    except Exception as error:  # noqa: BLE001
        raise _http(error) from error


def _manage(body: ManageBody, action, **kwargs) -> dict[str, Any]:
    try:
        require_confirmation(body.step_up)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    try:
        with begin(_engine) as connection:
            _actor(connection, body.project_id, body.actor, MANAGE)
            return action(connection, project_id=body.project_id, actor_kind="person", **kwargs)
    except Exception as error:  # noqa: BLE001
        raise _http(error) from error


@router.post("/facts/{fact_id}/promote")
@permit(P.KNOWLEDGE_MANAGE, scope=Scope.project(body="project_id"))
def promote(fact_id: str, body: ManageBody) -> dict[str, Any]:
    return _manage(body, _service.promote, fact_id=fact_id)


@router.post("/facts/{fact_id}/reject")
@permit(P.KNOWLEDGE_MANAGE, scope=Scope.project(body="project_id"))
def reject(fact_id: str, body: ManageBody) -> dict[str, Any]:
    return _manage(body, _service.reject, fact_id=fact_id)


@router.post("/facts/{fact_id}/forget")
@permit(P.KNOWLEDGE_MANAGE, scope=Scope.project(body="project_id"))
def forget(fact_id: str, body: ManageBody) -> dict[str, Any]:
    return _manage(body, _service.forget, fact_id=fact_id)


class IngestTickBody(BaseModel):
    sweep: bool = False


@router.post("/ingest/tick")
@permit(P.WORKER_LEASE)
def ingest_tick(body: Optional[IngestTickBody] = None) -> dict[str, Any]:
    """The worker's loop: capture the runs and incidents that just finished (event-driven ingest,
    services/knowledge/ingest.py). No model is called."""
    from apps.api.app.main import require_worker_principal
    from boss_engine.services.knowledge.ingest import tick

    require_worker_principal()
    return tick(_engine, sweep=bool(body and body.sweep))


class CypherBody(BaseModel):
    project_id: str
    actor: str
    query: str
    params: Optional[dict[str, Any]] = None
    columns: Optional[list[str]] = None
    limit: int = 200


@router.post("/cypher")
@permit(P.KNOWLEDGE_READ, scope=Scope.project(body="project_id"), readonly_post=True)
def cypher(body: CypherBody) -> dict[str, Any]:
    """Read-only Cypher over the project's graph: writes refused, 5 s, a row limit. A POST only
    because a query does not fit a URL; it writes nothing."""
    try:
        with begin(_engine) as connection:
            _actor(connection, body.project_id, body.actor, READ)
            return _service.cypher(connection, project_id=body.project_id, query=body.query,
                                   params=body.params, columns=body.columns, limit=body.limit)
    except TimeoutError as error:
        raise HTTPException(status_code=408, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:  # noqa: BLE001
        raise _http(error) from error


@router.get("/repositories")
@permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
def repositories(project_id: str = Query(...), actor: str = Query(...)) -> dict[str, Any]:
    """Each registered repository's code graph (services/knowledge/repo_index.py)."""
    from boss_engine.services.knowledge.repo_index import status as repo_status

    try:
        with begin(_engine) as connection:
            _actor(connection, project_id, actor, READ)
            return repo_status(connection, project_id)
    except Exception as error:  # noqa: BLE001
        raise _http(error) from error


class RepositoryIndexBody(BaseModel):
    project_id: str
    actor: str
    repository: str = ""
    force: bool = False


@router.post("/repositories/index")
# A read-safe action (operator decision 2026-10-24): it changes no record anyone made. It queues
# a refresh of derived data — parsing the repository's branch into the graph, no model, bounded,
# one in flight per repository — so whoever may read the graph may ask for it to be current.
@permit(P.KNOWLEDGE_READ, scope=Scope.project(body="project_id"), readonly_post=True)
def index_repositories(body: RepositoryIndexBody) -> dict[str, Any]:
    """Index now: the project's repositories, or the one named (owner/name or an id)."""
    from boss_engine.services.knowledge.repo_index import request as repo_request

    try:
        with begin(_engine) as connection:
            _actor(connection, body.project_id, body.actor, READ)
            return repo_request(connection, _settings, project_id=body.project_id, actor=body.actor,
                                repository=body.repository, force=body.force)
    except Exception as error:  # noqa: BLE001
        raise _http(error) from error


class RepositoryIndexExecuteBody(BaseModel):
    job_id: str
    worker_id: str
    lease_token: str


@router.post("/repositories/index/execute")
@permit(P.WORKER_LEASE)
def execute_repository_index(body: RepositoryIndexExecuteBody) -> dict[str, Any]:
    """The worker hands its leased `knowledge.repository.index` job here: the GitHub credential
    and the graph are in this container. Fenced by the lease; parsing only, no model."""
    from apps.api.app.main import require_worker_principal
    from boss_engine.services.knowledge.repo_index import execute_job

    require_worker_principal()
    return execute_job(_engine, _settings, job_id=body.job_id, worker_id=body.worker_id,
                       lease_token=body.lease_token)
