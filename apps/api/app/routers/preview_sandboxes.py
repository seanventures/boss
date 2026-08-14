"""Persistent preview sandboxes API (BOSS-11 / Workload B).

Durable reservations for long-lived preview apps on the sandbox host.
Fail closed without a host unless ``simulate=true`` (tests / local demos).
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from boss_engine.services.preview_sandbox_service import PreviewSandboxService

from ..config import get_settings
from ..db import begin, get_engine

router = APIRouter(prefix="/v1/preview-sandboxes", tags=["preview-sandboxes"])


class ReserveRequest(BaseModel):
    project_id: str
    app_id: str
    actor: str
    run_id: Optional[str] = None
    repo: Optional[str] = None
    branch: Optional[str] = None
    image_or_build: Optional[str] = None
    run_cmd: Optional[str] = None
    env: dict[str, str] = Field(default_factory=dict)
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    ttl_seconds: int = Field(default=3600, ge=60, le=604800)
    simulate: bool = False


class HeartbeatRequest(BaseModel):
    actor: str
    ttl_seconds: Optional[int] = Field(default=None, ge=60, le=604800)


class ReleaseRequest(BaseModel):
    actor: str


def _svc() -> PreviewSandboxService:
    return PreviewSandboxService(get_settings())


@router.post("")
@router.post("/")
def reserve_preview_sandbox(payload: ReserveRequest) -> dict[str, Any]:
    """Reserve a durable preview sandbox (host-backed or simulated)."""
    try:
        with begin(get_engine()) as connection:
            return _svc().reserve(
                connection,
                project_id=payload.project_id,
                app_id=payload.app_id,
                actor=payload.actor,
                run_id=payload.run_id,
                repo=payload.repo,
                branch=payload.branch,
                image_or_build=payload.image_or_build,
                run_cmd=payload.run_cmd,
                env=payload.env,
                port=payload.port,
                ttl_seconds=payload.ttl_seconds,
                simulate=payload.simulate,
            )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except RuntimeError as error:
        # Host missing / unreachable — fail closed.
        raise HTTPException(status_code=503, detail=str(error)) from error


@router.get("")
@router.get("/")
def list_preview_sandboxes(
    project_id: str = Query(...),
    limit: int = Query(50, ge=1, le=200),
    include_expired: bool = Query(False),
) -> dict[str, Any]:
    """List durable preview sandbox records for a project (reaps expired)."""
    with begin(get_engine()) as connection:
        items = _svc().list(
            connection,
            project_id,
            limit=limit,
            include_expired=include_expired,
        )
    return {"project_id": project_id, "items": items, "count": len(items)}


@router.get("/{preview_id}")
def get_preview_sandbox(
    preview_id: str,
    project_id: Optional[str] = Query(None),
) -> dict[str, Any]:
    """Status of one durable preview sandbox."""
    try:
        with begin(get_engine()) as connection:
            return _svc().get(
                connection, preview_id, project_id=project_id
            )
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/{preview_id}/heartbeat")
def heartbeat_preview_sandbox(
    preview_id: str, payload: HeartbeatRequest
) -> dict[str, Any]:
    """TTL heartbeat — extends reservation; fail closed if released/failed/host down."""
    try:
        with begin(get_engine()) as connection:
            return _svc().heartbeat(
                connection,
                preview_id,
                actor=payload.actor,
                ttl_seconds=payload.ttl_seconds,
            )
    except ValueError as error:
        detail = str(error)
        code = 404 if "not found" in detail else 422
        raise HTTPException(status_code=code, detail=detail) from error
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@router.post("/{preview_id}/release")
@router.delete("/{preview_id}")
def release_preview_sandbox(
    preview_id: str, payload: Optional[ReleaseRequest] = None
) -> dict[str, Any]:
    """Release a preview sandbox (host DELETE + durable status=released)."""
    actor = (payload.actor if payload else "") or ""
    if not actor.strip():
        # DELETE may omit body; require actor via query is awkward — require body.
        raise HTTPException(status_code=422, detail="actor is required")
    try:
        with begin(get_engine()) as connection:
            return _svc().release(
                connection, preview_id, actor=actor.strip()
            )
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
