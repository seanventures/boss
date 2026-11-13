"""Decide from Slack and phone (BOSSAPP-833/834): chat endpoints, the signed public door, links.

Domain logic lives in ``boss_engine.services.chat_channels`` (and ``chat_adapters``); routes stay
thin.

THE PUBLIC ROUTE IS UNDER /v1/webhooks/inbound/ (exempt from transport auth, like every signed
machine callback). What authenticates a request is the CHANNEL's proof, checked first by
``chat_channels.verify_request`` (the declared verifier): Slack's ``X-Slack-Signature`` over
``v0:<timestamp>:<raw body>`` with the app's signing secret, within 5 minutes; Telegram's
``X-Telegram-Bot-Api-Secret-Token`` equal to the secret registered with ``setWebhook``. One URL per
endpoint serves Slack's Interactivity AND Event Subscriptions (a form body is an interaction, a
JSON body an event). The endpoint id only finds the endpoint.

Management routes are project-scoped (``@permit`` integration:read / integration:manage, and the
legacy member check), and every read is by id AND project_id. Linking is a PERSON's own act:
``@authenticated(people_only)`` — the signed-in person confirms "this chat user is me".
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public
from boss_engine.services import chat_channels as chat
from boss_engine.services.chat_adapters import ChatVerifyRefused, adapter_for

from ..db import begin, get_engine
from .identity_me import need_person_session

router = APIRouter(tags=["chat-channels"])

MAX_BODY_BYTES = 256 * 1024


class ChatEndpointCreateBody(BaseModel):
    project_id: str
    kind: str = Field(description="slack | telegram | discord (discord is a stub)")
    name: str = ""
    config: dict[str, Any] = Field(default_factory=dict, description="channel (Slack channel id "
                                   "or Telegram chat id), max_tier (<= 2)")
    secrets: dict[str, str] = Field(default_factory=dict, description="slack: signing_secret, "
                                    "bot_token; telegram: bot_token, webhook_secret. Stored in the "
                                    "secret backend; never returned.")
    actor: str = "executive_console"


class ChatEndpointStatusBody(BaseModel):
    project_id: str
    status: str
    actor: str = "executive_console"


class ChatLinkCodeBody(BaseModel):
    code: str


def _settings():
    from boss_engine.config import get_settings

    return get_settings()


def _write_actor(connection, project_id: str, actor: str) -> str:
    from apps.api.app.main import require_project_write_actor

    return require_project_write_actor(connection, project_id, actor, "integration_manage")


def _reader(connection, project_id: str) -> None:
    from apps.api.app.main import require_project_reader

    require_project_reader(connection, project_id)


def _refusal(error: chat.ChatRefused) -> JSONResponse:
    return JSONResponse(status_code=error.status,
                        content={"error": error.code, "message": error.message})


# ---- management ---------------------------------------------------------------------------------

@router.get("/v1/chat/endpoints")
@permit(P.INTEGRATION_READ, scope=Scope.project(query="project_id"))
def list_chat_endpoints(project_id: str) -> dict:
    with begin(get_engine()) as connection:
        _reader(connection, project_id)
        return {"project_id": project_id,
                "endpoints": chat.list_endpoints(connection, project_id, _settings())}


@router.post("/v1/chat/endpoints", status_code=201)
@permit(P.INTEGRATION_MANAGE, scope=Scope.project(body="project_id"))
def create_chat_endpoint(payload: ChatEndpointCreateBody):
    """Connect a Slack app or Telegram bot. The response names the URL(s) to paste into the
    channel's own settings; the secrets go to the secret backend and are never returned."""
    try:
        with begin(get_engine()) as connection:
            actor = _write_actor(connection, payload.project_id, payload.actor)
            return chat.create_endpoint(connection, _settings(), project_id=payload.project_id,
                                        kind=payload.kind, name=payload.name,
                                        config=payload.config, secrets=payload.secrets,
                                        actor=actor)
    except chat.ChatRefused as error:
        return _refusal(error)


@router.post("/v1/chat/endpoints/{endpoint_id}/status")
@permit(P.INTEGRATION_MANAGE, scope=Scope.project(body="project_id"))
def set_chat_endpoint_status(endpoint_id: str, payload: ChatEndpointStatusBody):
    try:
        with begin(get_engine()) as connection:
            actor = _write_actor(connection, payload.project_id, payload.actor)
            return chat.set_endpoint_status(connection, payload.project_id, endpoint_id,
                                            payload.status, actor)
    except chat.ChatRefused as error:
        return _refusal(error)


# ---- linking (a person's own act) ------------------------------------------------------------

def _signed_in(request: Request, db: Any):
    """(the request's AuthContext, None) or (None, the refusal to answer with)."""
    from boss_engine.identity.authn import AuthnError

    from .identity_auth import current_context, refusal

    try:
        return current_context(request, db), None
    except AuthnError as exc:
        return None, refusal(exc)


@router.get("/v1/chat/links")
@authenticated(self_only=True, people_only=True, note="the caller's own chat links only")
def my_chat_links(request: Request):
    with begin(get_engine()) as connection:
        ctx, blocked = _signed_in(request, connection)
        blocked = blocked or need_person_session(ctx)
        if blocked is not None:
            return blocked
        return {"links": chat.links_of(connection, ctx.principal.id)}


@router.post("/v1/chat/links/peek")
@authenticated(self_only=True, people_only=True,
               note="reads what a one-time link code would link; spends nothing")
def peek_chat_link(payload: ChatLinkCodeBody, request: Request):
    with begin(get_engine()) as connection:
        ctx, blocked = _signed_in(request, connection)
        blocked = blocked or need_person_session(ctx)
        if blocked is not None:
            return blocked
        try:
            return chat.peek_link(connection, payload.code)
        except chat.ChatRefused as error:
            return _refusal(error)


@router.post("/v1/chat/links/confirm")
@authenticated(self_only=True, people_only=True,
               note="links the code's chat user to the signed-in person (never anyone else)")
def confirm_chat_link(payload: ChatLinkCodeBody, request: Request):
    """The handshake's end: the signed-in person says "that chat user is me". From now on their
    presses in that channel answer decisions AS them (and only within what they may decide)."""
    try:
        with begin(get_engine()) as connection:
            ctx, blocked = _signed_in(request, connection)
            blocked = blocked or need_person_session(ctx)
            if blocked is not None:
                return blocked
            return chat.confirm_link(connection, payload.code, ctx.principal)
    except chat.ChatRefused as error:
        return _refusal(error)


@router.post("/v1/chat/links/{link_id}/revoke")
@authenticated(self_only=True, people_only=True, note="revokes one of the caller's own links")
def revoke_chat_link(link_id: str, request: Request):
    with begin(get_engine()) as connection:
        ctx, blocked = _signed_in(request, connection)
        blocked = blocked or need_person_session(ctx)
        if blocked is not None:
            return blocked
        if not chat.revoke_link(connection, ctx.principal.id, link_id):
            raise HTTPException(status_code=404, detail="No such link of yours")
        return {"revoked": link_id}


# ---- the signed public door -------------------------------------------------------------------

async def _read_bounded(request: Request) -> tuple[bytes, bool]:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return b"", True
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return b"", True
        chunks.append(chunk)
    return b"".join(chunks), False


def _receive(endpoint_id: str, headers: dict[str, str], raw: bytes) -> dict:
    """Verify, parse and act in ONE transaction; then (committed) open a form and send what the
    press queued. Runs in the threadpool: it is all blocking I/O."""
    from boss_engine.services.notify import deliver_chat_due

    engine = get_engine()
    settings = _settings()
    with begin(engine) as connection:
        endpoint = chat.get_endpoint(connection, endpoint_id)
        try:
            chat.verify_request(connection, settings, endpoint, headers, raw)
        except ChatVerifyRefused as refused:
            if endpoint is not None and refused.reason != "unknown_endpoint":
                from boss_engine.services.core import emit_event

                emit_event(connection, endpoint["project_id"], "chat.request_refused",
                           chat.ACTOR, "chat_endpoint", endpoint_id,
                           {"reason": refused.reason, "kind": endpoint["kind"]})
            return {"status": "verify_refused", "reason": refused.reason}
        adapter = adapter_for(endpoint["kind"])
        try:
            inbound = adapter.parse(headers, raw)
        except ValueError:
            return {"status": "bad_body"}
        result = chat.handle_inbound(connection, settings, endpoint, inbound)
    if result.get("status") == "note_required":
        try:
            with begin(engine) as connection:
                chat.open_note_form(connection, settings, endpoint, result, inbound)
        except Exception as exc:  # noqa: BLE001 — the press is answered in words instead
            result = {"status": "console_required",
                      "words": f"Could not open the note form ({type(exc).__name__}). "
                               "Answer this decision in BOSS."}
    try:
        deliver_chat_due(engine, settings, endpoint_id=endpoint_id, limit=20)
    except Exception:  # noqa: BLE001 — the outbox tick retries; the press itself is recorded
        pass
    result["kind"] = endpoint["kind"]
    result["inbound"] = inbound.kind
    return result


@router.post("/v1/webhooks/inbound/chat/{endpoint_id}")
@public(reason=PublicReason.HMAC_SIGNED_WEBHOOK, verifier=chat.verify_request)
async def receive_chat(endpoint_id: str, request: Request):
    """A Slack interaction or event, or a Telegram update, for one chat endpoint. Signed by the
    channel (see the module docstring); 401 when the proof fails, 404 for an unknown endpoint."""
    raw, too_large = await _read_bounded(request)
    if too_large:
        return JSONResponse(status_code=413, content={"error": "too_large"})
    headers = {key.lower(): value for key, value in request.headers.items()}
    result = await run_in_threadpool(_receive, endpoint_id, headers, raw)
    status = result.get("status")
    if status == "verify_refused":
        code = 404 if result["reason"] == "unknown_endpoint" else 401
        return JSONResponse(status_code=code, content={"error": result["reason"]})
    if status == "bad_body":
        return JSONResponse(status_code=400, content={"error": "bad_body"})
    if status == "handshake":
        return JSONResponse({"challenge": result.get("challenge", "")})
    if result.get("kind") == "slack" and result.get("inbound") == "note_submitted":
        if status in ("done", "pending_second"):
            return JSONResponse({"response_action": "clear"})
        return JSONResponse({"response_action": "errors",
                             "errors": {"note": str(result.get("words") or "Refused")[:150]}})
    body = {"ok": True, "status": status}
    if result.get("code"):
        body["code"] = result["code"]
    return JSONResponse(body)


__all__ = ["router"]
