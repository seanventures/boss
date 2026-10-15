"""Webhook triggers for workflows (#512): the owner's management routes and the public door.

Domain logic lives in ``boss_engine.services.workflow_webhooks``; these routes stay thin.

THE PUBLIC ROUTE IS UNDER /v1/webhooks/inbound/. That prefix is the one place signed machine
callbacks are already exempt from transport auth, so a sender with no account here can reach it;
what authenticates the request is the HMAC signature over its timestamp and raw body, checked
in ``workflow_webhooks.receive`` (declared as the route's verifier). The path token only finds
the trigger.

Every management route is project-scoped twice: ``@permit(..., Scope.project(...))`` and
``require_project_actor``; the service then reads the trigger by ``id AND project_id``, so one
project's id never reaches another project's trigger.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from boss_engine.identity import P, PublicReason, Scope, permit, public
from boss_engine.services import workflow_webhooks as hooks

from ..db import begin, get_engine

router = APIRouter(tags=["workflow-webhooks"])

#: HTTP status for each refusal a sender can cause. The body names the reason too.
_REFUSAL_STATUS = {
    "disabled": 409,
    "too_large": 413,
    "unsupported_content_type": 415,
    "missing_signature": 401,
    "stale_timestamp": 401,
    "bad_signature": 401,
    "invalid_json": 400,
    "not_ready": 409,
    "workflow_missing": 409,
    "actor_not_authorized": 403,
    "rate_limited": 429,
    "start_failed": 503,
}


class WebhookTriggerCreateBody(BaseModel):
    project_id: str
    workflow_key: str
    name: Optional[str] = None
    actor: str = "executive_console"


class WebhookTriggerActionBody(BaseModel):
    project_id: str
    actor: str = "executive_console"


class WebhookTriggerEnabledBody(BaseModel):
    project_id: str
    enabled: bool = True
    actor: str = "executive_console"


class WebhookTrigger(BaseModel):
    id: str
    project_id: str
    workflow_key: str
    name: str
    path: str = Field(description="POST signed JSON here. The token in it finds the trigger.")
    enabled: bool
    secret_last4: Optional[str] = None
    secret_rotated_at: Optional[Any] = None
    refused_unrecorded: int = 0
    last_error: Optional[str] = Field(default=None, description="Why BOSS turned it off, e.g. Renew authority.")
    on_behalf_of: Optional[str] = None
    last_delivery_at: Optional[Any] = None
    created_at: Optional[Any] = None
    actor: str


class WebhookTriggerWithSecret(WebhookTrigger):
    secret: str = Field(description="The signing secret. Returned only here, once.")


class WebhookTriggerList(BaseModel):
    webhooks: list[WebhookTrigger]


class WebhookDelivery(BaseModel):
    id: str
    delivery_id: Optional[str] = None
    status: str
    reason: Optional[str] = None
    run_id: Optional[str] = None
    body_bytes: Optional[int] = None
    received_at: Any


class WebhookDeliveryList(BaseModel):
    trigger: WebhookTrigger
    deliveries: list[WebhookDelivery]


class WebhookInboundResponse(BaseModel):
    status: str
    run_id: Optional[str] = None
    delivery_id: Optional[str] = None


def _require(connection, project_id: str, actor: str, permission: str) -> None:
    from apps.api.app.main import require_project_actor

    require_project_actor(connection, project_id, actor, permission)


@router.get("/v1/delivery-workflows/webhooks", response_model=WebhookTriggerList)
@permit(P.WORKFLOW_READ, scope=Scope.project(query="project_id"))
def list_workflow_webhooks(project_id: str, workflow_key: Optional[str] = None,
                           actor: str = "executive_console") -> dict:
    with begin(get_engine()) as connection:
        _require(connection, project_id, actor, "workflow_manage")
        return {"webhooks": hooks.list_triggers(connection, project_id, workflow_key)}


@router.post("/v1/delivery-workflows/webhooks", status_code=201,
             response_model=WebhookTriggerWithSecret)
@permit(P.WORKFLOW_SCHEDULE_MANAGE, scope=Scope.project(body="project_id"))
def create_workflow_webhook(payload: WebhookTriggerCreateBody) -> dict:
    """A new trigger. The response is the only time the signing secret is shown (besides rotate)."""
    try:
        with begin(get_engine()) as connection:
            _require(connection, payload.project_id, payload.actor, "workflow_manage")
            # It will start runs unattended, like an enabled schedule.
            _require(connection, payload.project_id, payload.actor, "workflow_execute")
            return hooks.create(connection, project_id=payload.project_id,
                                workflow_key=payload.workflow_key, name=payload.name or "",
                                actor=payload.actor)
    except hooks.WebhookTriggerError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/v1/delivery-workflows/webhooks/{trigger_id}/rotate",
             response_model=WebhookTriggerWithSecret)
@permit(P.WORKFLOW_SCHEDULE_MANAGE, scope=Scope.project(body="project_id"))
def rotate_workflow_webhook_secret(trigger_id: str, payload: WebhookTriggerActionBody) -> dict:
    """A new signing secret, shown once. The old one stops verifying immediately."""
    try:
        with begin(get_engine()) as connection:
            _require(connection, payload.project_id, payload.actor, "workflow_manage")
            return hooks.rotate(connection, project_id=payload.project_id,
                                trigger_id=trigger_id, actor=payload.actor)
    except hooks.WebhookTriggerNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/v1/delivery-workflows/webhooks/{trigger_id}/enabled",
             response_model=WebhookTrigger)
@permit(P.WORKFLOW_SCHEDULE_MANAGE, scope=Scope.project(body="project_id"))
def set_workflow_webhook_enabled(trigger_id: str, payload: WebhookTriggerEnabledBody) -> dict:
    try:
        with begin(get_engine()) as connection:
            _require(connection, payload.project_id, payload.actor, "workflow_manage")
            if payload.enabled:
                _require(connection, payload.project_id, payload.actor, "workflow_execute")
            return hooks.set_enabled(connection, project_id=payload.project_id,
                                     trigger_id=trigger_id, enabled=payload.enabled,
                                     actor=payload.actor)
    except hooks.WebhookTriggerNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/v1/delivery-workflows/webhooks/{trigger_id}/deliveries",
            response_model=WebhookDeliveryList)
@permit(P.WORKFLOW_READ, scope=Scope.project(query="project_id"))
def list_workflow_webhook_deliveries(trigger_id: str, project_id: str, limit: int = 50,
                                     actor: str = "executive_console") -> dict:
    try:
        with begin(get_engine()) as connection:
            _require(connection, project_id, actor, "workflow_manage")
            return hooks.deliveries(connection, project_id=project_id, trigger_id=trigger_id,
                                    limit=limit)
    except hooks.WebhookTriggerNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


async def _read_bounded(request: Request) -> tuple[bytes, bool]:
    """The raw body, stopping at MAX_BODY_BYTES + 1 so an oversized one is never held whole."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > hooks.MAX_BODY_BYTES:
        return b"", True
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > hooks.MAX_BODY_BYTES:
            return b"", True
        chunks.append(chunk)
    return b"".join(chunks), False


@router.post("/v1/webhooks/inbound/workflow/{path_token}", status_code=202,
             response_model=WebhookInboundResponse)
@public(reason=PublicReason.HMAC_SIGNED_WEBHOOK, verifier=hooks.receive)
async def receive_workflow_webhook(path_token: str, request: Request):
    """Start the trigger's workflow with this JSON body as the run's input.

    Signed: ``X-BOSS-Signature: v1=<hex HMAC-SHA256(secret, "<timestamp>.<raw body>")>`` and
    ``X-BOSS-Timestamp: <unix seconds>``. ``X-BOSS-Delivery`` (optional) is the dedupe key.
    202 started, 200 duplicate, 404 unknown URL; every refusal is recorded on the trigger.
    """
    raw_body, too_large = await _read_bounded(request)
    headers = {key.lower(): value for key, value in request.headers.items()}
    # The refusal row is written and COMMITTED before the error goes back: raising inside the
    # transaction would roll back the very record that makes the refusal visible.
    with begin(get_engine()) as connection:
        result = hooks.receive(connection, path_token, raw_body, headers,
                               body_too_large=too_large)
    status = result["status"]
    if status == "not_found":
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    if status == "refused":
        return JSONResponse(status_code=_REFUSAL_STATUS.get(result["reason"], 400),
                            content={"status": "refused", "reason": result["reason"],
                                     "detail": result.get("detail")})
    body = {"status": status, "run_id": result.get("run_id"),
            "delivery_id": result.get("delivery_id")}
    return JSONResponse(status_code=200 if status == "duplicate" else 202, content=body)


__all__ = ["router"]
