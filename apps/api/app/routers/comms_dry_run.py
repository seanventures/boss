"""Isolated Community comms dry-run close-the-loop.

``POST /v1/comms/outbox/{id}/deliver`` is the real worker path (budget +
``comms.outbox.deliver`` + adapters). This router adds a no-send dry-run
without touching ``main.py``.
"""
from __future__ import annotations

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..db import begin, get_engine


router = APIRouter(tags=["comms-dry-run"])


class OutboxDryRunRequest(BaseModel):
    project_id: str
    actor: str = "executive_console"
    dedupe_key: Optional[str] = Field(default=None)


class OutboxDryRunResponse(BaseModel):
    status: str
    message_id: Optional[str] = None
    receipt_id: Optional[str] = None
    delivery_mode: str = "dry_run"
    provider_send: bool = False
    message: str = "dry-run — no provider send"


@router.post(
    "/v1/comms/outbox/{message_id}/dry-run",
    response_model=OutboxDryRunResponse,
)
@permit(P.COMMS_SEND, scope=Scope.project(body="project_id"))
def dry_run_outbox_message(
    message_id: str, payload: OutboxDryRunRequest
) -> OutboxDryRunResponse:
    from apps.api.app.main import comms, require_project_actor
    from boss_engine.services.comms import (
        OutboxDryRunNotAllowedError,
        OutboxMessageNotFoundError,
    )

    with begin(get_engine()) as connection:
        try:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "comms_manage",
            )
            result = comms.dry_run_deliver_outbox(
                connection,
                payload.project_id,
                message_id,
                payload.actor,
                payload.dedupe_key,
            )
        except OutboxMessageNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except OutboxDryRunNotAllowedError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return OutboxDryRunResponse(**result)


__all__ = [
    "router",
    "OutboxDryRunRequest",
    "OutboxDryRunResponse",
]
