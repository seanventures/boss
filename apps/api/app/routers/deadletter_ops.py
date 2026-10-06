"""DLQ operator remediations — list, attach, retry/requeue without editing main.py.

Classify + incident + requeue already live on ``/v1/deadletters*`` in main.py.
This router adds first-class remediations so every operator action is durable.
"""
from __future__ import annotations

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from boss_engine.services.deadletters import (
    REMEDIATION_ACTIONS,
    DeadletterRemediationService,
)

from ..config import get_settings
from ..db import begin, get_engine


router = APIRouter(tags=["deadletter-ops"])
_service: Optional[DeadletterRemediationService] = None


def _svc() -> DeadletterRemediationService:
    global _service
    if _service is None:
        _service = DeadletterRemediationService(get_settings())
    return _service


class RemediationCreateRequest(BaseModel):
    actor: str = "system"
    action: str = "retry"
    reason: str = ""
    run_after_seconds: int = Field(default=0, ge=0, le=3600)


class RemediationRetryRequest(BaseModel):
    actor: str = "system"
    reason: str = "operator_requested_retry"
    run_after_seconds: int = Field(default=0, ge=0, le=3600)


def _require_actor(connection: Any, project_id: str, actor: str) -> None:
    from apps.api.app.main import require_project_actor

    require_project_actor(connection, project_id, actor, "task_create")


def _project_for_deadletter(connection: Any, deadletter_id: str) -> str:
    from sqlalchemy import text

    row = connection.execute(
        text("SELECT project_id FROM deadletters WHERE id = :deadletter_id"),
        {"deadletter_id": deadletter_id},
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="deadletter not found")
    return row["project_id"]


@router.get("/v1/deadletters/remediations")
@permit(P.JOB_READ, scope=Scope.project(query="project_id"))
def list_deadletter_remediations(
    project_id: str = Query(...),
    deadletter_id: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    """List remediations for a project (optionally one deadletter). Empty is valid."""
    with begin(get_engine()) as connection:
        try:
            return _svc().list_remediations(
                connection,
                project_id=project_id,
                deadletter_id=deadletter_id,
                limit=limit,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/v1/deadletters/{deadletter_id}/remediations", status_code=201)
@permit(P.DEADLETTER_MANAGE, scope=Scope.of("deadletter", path="deadletter_id"))
def create_deadletter_remediation(
    deadletter_id: str, payload: RemediationCreateRequest
) -> dict[str, Any]:
    """Attach a remediation to a DLQ item and apply it (retry/requeue/escalate/hold)."""
    action = (payload.action or "").strip().lower()
    if action not in REMEDIATION_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail="action must be one of: " + ", ".join(REMEDIATION_ACTIONS),
        )
    with begin(get_engine()) as connection:
        project_id = _project_for_deadletter(connection, deadletter_id)
        _require_actor(connection, project_id, payload.actor)
        try:
            rem = _svc().create_remediation(
                connection,
                deadletter_id=deadletter_id,
                actor=payload.actor,
                action=action,
                reason=payload.reason,
                run_after_seconds=payload.run_after_seconds,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {
            "remediation": rem,
            "deadletter_id": deadletter_id,
            "project_id": project_id,
            "status": rem["status"],
        }


@router.post("/v1/deadletters/{deadletter_id}/retry")
@permit(P.DEADLETTER_MANAGE, scope=Scope.of("deadletter", path="deadletter_id"))
def retry_deadletter(
    deadletter_id: str, payload: RemediationRetryRequest
) -> dict[str, Any]:
    """Retry a terminal job deadletter. Always writes a remediation (applied/blocked/failed)."""
    with begin(get_engine()) as connection:
        project_id = _project_for_deadletter(connection, deadletter_id)
        _require_actor(connection, project_id, payload.actor)
        try:
            rem = _svc().retry_deadletter(
                connection,
                deadletter_id=deadletter_id,
                actor=payload.actor,
                reason=payload.reason,
                run_after_seconds=payload.run_after_seconds,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {
            "remediation": rem,
            "deadletter_id": deadletter_id,
            "project_id": project_id,
            "status": rem["status"],
            "requeued_job_id": rem.get("requeued_job_id"),
            "deadletter_status": (rem.get("result") or {}).get("deadletter_status"),
        }


__all__ = [
    "router",
    "RemediationCreateRequest",
    "RemediationRetryRequest",
]
