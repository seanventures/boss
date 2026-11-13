"""External memory backends over HTTP (BOSSAPP-892, services/memory_backends.py).

    GET  /v1/memory-backends                 the project's backends and what waits for review
    PUT  /v1/memory-backends                 add or change one (the credential is a NAMED env var)
    POST /v1/memory-backends/{id}/review     approve or reject queued snapshots; approved ones are
                                             sent at once (best effort; a failure is retried)
    POST /v1/memory-backends/{id}/sync       send what is approved now

Nothing leaves BOSS without a person's approval of the exact text. Mounted from main.py with the
auth helpers injected.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit

#: The legacy member permission the memory routes already check (main.py's /v1/memory).
_MANAGE = "agent_chat"


class BackendBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    name: str
    kind: str = "mem0"
    base_url: str
    credential_env: str = ""
    read_enabled: bool = False
    enabled: bool = True


class ReviewBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    export_ids: list[str] = Field(default_factory=list)
    decision: str


class SyncBody(BaseModel):
    project_id: str
    actor: Optional[str] = None


def build_memory_backends_router(
    *,
    engine,
    settings: Callable[[], Any],
    require_write_actor: Callable[..., str],
    require_reader: Callable[..., None],
) -> APIRouter:
    from boss_engine.services import memory_backends as mb

    router = APIRouter(tags=["memory"])

    def _fail(error: Exception):
        if isinstance(error, mb.MemoryBackendError):
            raise HTTPException(status_code=getattr(error, "status", 422),
                                detail=str(error)) from error
        raise error

    @router.get("/v1/memory-backends")
    @permit(P.MEMORY_READ, scope=Scope.project(query="project_id"))
    def list_backends(project_id: str) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            return {"project_id": project_id,
                    "backends": mb.list_backends(connection, project_id),
                    "awaiting_review": mb.pending(connection, project_id)}

    @router.put("/v1/memory-backends")
    @permit(P.MEMORY_MANAGE, scope=Scope.project(body="project_id"))
    def configure(body: BackendBody) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, _MANAGE)
            try:
                return mb.configure(connection, project_id=body.project_id, actor=actor,
                                    name=body.name, kind=body.kind, base_url=body.base_url,
                                    credential_env=body.credential_env,
                                    read_enabled=body.read_enabled, enabled=body.enabled)
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise

    def _sync(project_id: str, backend_id: str) -> dict[str, Any]:
        try:
            return mb.sync(engine, project_id=project_id, backend_id=backend_id)
        except mb.MemoryBackendError as error:
            return {"sent": 0, "failed": 0, "skipped": 0, "error": str(error)}

    @router.post("/v1/memory-backends/{backend_id}/review")
    @permit(P.MEMORY_MANAGE, scope=Scope.project(body="project_id"))
    def review(backend_id: str, body: ReviewBody) -> dict[str, Any]:
        """A person lets these exact snapshots leave, or keeps them in."""
        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, _MANAGE)
            try:
                mb.get_backend(connection, body.project_id, backend_id)
                result = mb.review(connection, project_id=body.project_id, actor=actor,
                                   export_ids=body.export_ids, decision=body.decision)
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise
        if result["status"] == "approved" and result["ids"]:
            result["sync"] = _sync(body.project_id, backend_id)
        return result

    @router.post("/v1/memory-backends/{backend_id}/sync")
    @permit(P.MEMORY_MANAGE, scope=Scope.project(body="project_id"))
    def sync(backend_id: str, body: SyncBody) -> dict[str, Any]:
        with begin(engine) as connection:
            require_write_actor(connection, body.project_id, body.actor, _MANAGE)
            try:
                mb.get_backend(connection, body.project_id, backend_id)
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise
        return _sync(body.project_id, backend_id)

    return router
