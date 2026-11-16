"""Visitor snapshots (BOSSAPP-1243). A pending publish is not the visitor URL.

The permit declaration is the gate. This module does not import the API app:
that import builds an engine at load time.
"""
from __future__ import annotations

from typing import Any, Optional

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from boss_engine.db import begin, get_engine
from boss_engine.services.publish_audience import (
    PublishAudienceError,
    approve_visitor_snapshot,
    current_visitor,
    get_visitor_snapshot,
    publish_visitor_snapshot,
)

router = APIRouter(tags=["visitor-snapshots"])


class PublishVisitorSnapshotRequest(BaseModel):
    audience: str
    actor: str
    snapshot: dict[str, Any]
    approved: bool = False
    source_preview_id: Optional[str] = None
    preview_url: Optional[str] = None
    dedupe_key: Optional[str] = None


class ApproveVisitorSnapshotRequest(BaseModel):
    actor: str


def _call(work):
    try:
        with begin(get_engine()) as connection:
            return work(connection)
    except PublishAudienceError as error:
        raise HTTPException(status_code=error.status, detail=str(error)) from error


@router.post("/v1/projects/{project_id}/visitor-snapshots", status_code=201)
@permit(P.PREVIEW_MANAGE, scope=Scope.project(path="project_id"))
def publish_snapshot(project_id: str, payload: PublishVisitorSnapshotRequest) -> dict[str, Any]:
    """Freeze a visitor plate. Without approval it stays pending and has no visitor URL."""
    return _call(lambda connection: publish_visitor_snapshot(
        connection,
        project_id=project_id,
        audience=payload.audience,
        actor=payload.actor,
        snapshot=payload.snapshot,
        approved=payload.approved,
        source_preview_id=payload.source_preview_id,
        preview_url=payload.preview_url,
        dedupe_key=payload.dedupe_key,
    ))


@router.post("/v1/projects/{project_id}/visitor-snapshots/{snapshot_id}/approve")
@permit(P.PREVIEW_APPROVE, scope=Scope.project(path="project_id"))
def approve_snapshot(project_id: str, snapshot_id: str,
                     payload: ApproveVisitorSnapshotRequest) -> dict[str, Any]:
    return _call(lambda connection: approve_visitor_snapshot(
        connection, project_id=project_id, snapshot_id=snapshot_id, actor=payload.actor))


@router.get("/v1/projects/{project_id}/visitor-snapshots/{snapshot_id}")
@permit(P.PREVIEW_READ, scope=Scope.project(path="project_id"))
def read_snapshot(project_id: str, snapshot_id: str) -> dict[str, Any]:
    def work(connection):
        row = get_visitor_snapshot(
            connection, project_id=project_id, snapshot_id=snapshot_id)
        if row is None:
            raise PublishAudienceError(f"no such visitor snapshot: {snapshot_id}", status=404)
        return row

    return _call(work)


@router.get("/v1/projects/{project_id}/visitor-url")
@permit(P.PREVIEW_READ, scope=Scope.project(path="project_id"))
def read_visitor_url(project_id: str, audience: str) -> dict[str, Any]:
    """The approved door for this audience. A pending plate is not returned."""
    def work(connection):
        row = current_visitor(connection, project_id=project_id, audience=audience)
        if row is None:
            return {"audience": audience.strip().lower(), "visitor_url": None,
                    "snapshot_id": None, "approval_state": None}
        return {"audience": row["audience"], "visitor_url": row["visitor_url"],
                "snapshot_id": row["id"], "approval_state": row["approval_state"]}

    return _call(work)
