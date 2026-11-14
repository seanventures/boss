"""A2A v1.0 OVER HTTP: a deployed agent's card and JSON-RPC endpoint (AP-P5, BOSSAPP-1056;
AGENT_PLATFORM §4.5; services/a2a_server.py holds the behaviour, D11 the exposure rule).

    GET  /a2a/{agent_id}/.well-known/agent-card.json   the signed card (public, like a JWKS or a
    GET  /.well-known/agent-card.json?agent_id=…        discovery document: it holds no secret)
    POST /a2a/{agent_id}                               JSON-RPC (SendMessage, SendStreamingMessage
                                                        → SSE, GetTask, CancelTask, push configs)
    PUT  /v1/agents/{role_key}/deployments/{environment}/a2a   turn A2A on/off (agent:deploy);
                                                        on is refused for agents with write tools
                                                        until strong isolation exists (D11)

The JSON-RPC route is `agent:invoke` at the agent's project (the agent id resolves it): a service
account's key. An agent not exposed answers 404, exactly like an unknown one.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit, public
from boss_engine.identity.declarations import PublicReason


class ExposureBody(BaseModel):
    project_id: str
    enabled: bool
    actor: Optional[str] = None


def _base_url(request: Request) -> str:
    import os

    configured = (os.environ.get("BOSS_PUBLIC_URL") or os.environ.get("BOSS_MCP_OAUTH_ISSUER")
                  or "").rstrip("/")
    return configured or str(request.base_url).rstrip("/")


def build_a2a_router(*, engine, require_write_actor: Callable[..., str]) -> APIRouter:
    from boss_engine.services import a2a_server

    router = APIRouter(tags=["a2a"])

    def _exposed(agent_id: str) -> dict:
        with begin(engine) as connection:
            agent = a2a_server.resolve(connection, agent_id)
            if agent is None or a2a_server.exposure_refusal(connection, agent):
                raise HTTPException(status_code=404, detail="Not Found")
            return agent

    def _card(agent_id: str, request: Request) -> JSONResponse:
        agent = _exposed(agent_id)
        with begin(engine) as connection:
            card = a2a_server.agent_card(connection, agent, base_url=_base_url(request))
        return JSONResponse(card, headers={"Cache-Control": "public, max-age=300"})

    @router.get("/a2a/{agent_id}/.well-known/agent-card.json")
    @public(reason=PublicReason.JWKS)
    def agent_card(agent_id: str, request: Request):
        """The agent's signed A2A card (404 unless A2A is on for it)."""
        return _card(agent_id, request)

    @router.get("/.well-known/agent-card.json")
    @public(reason=PublicReason.JWKS)
    def agent_card_by_query(request: Request, agent_id: str = ""):
        """The same card, addressed by `?agent_id=` (the form LangGraph's Agent Server uses)."""
        if not agent_id:
            raise HTTPException(status_code=404, detail="Not Found")
        return _card(agent_id, request)

    @router.post("/a2a/{agent_id}")
    @permit(P.AGENT_INVOKE, scope=Scope.of("agent_identity", path="agent_id"))
    async def a2a_rpc(agent_id: str, request: Request):
        """A2A v1.0 JSON-RPC for one deployed agent."""
        from starlette.concurrency import run_in_threadpool

        from boss_engine.identity import lineage
        from boss_engine.kernel.trace import parse_traceparent
        from boss_engine.protocols import a2a_types as t

        try:
            a2a_server.check_version(request.headers.get("a2a-version")
                                     or request.query_params.get("A2A-Version"))
        except a2a_server.A2AError as exc:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": exc.error()})
        try:
            body = json.loads(await request.body() or b"null")
        except ValueError:
            return JSONResponse({"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32700, "message": "parse error"}})
        auth = lineage.current_context()
        tp = request.headers.get("traceparent")
        traceparent = tp.strip().lower() if parse_traceparent(tp) else None

        def prepare() -> tuple[dict, str]:
            agent = _exposed(agent_id)
            with begin(engine) as connection:
                actor = require_write_actor(connection, agent["project_id"], None,
                                            "command_submit")
            return agent, actor

        agent, actor = await run_in_threadpool(prepare)
        method = t.method_name(str((body or {}).get("method") or "")) \
            if isinstance(body, dict) else ""
        if method == "SendStreamingMessage":
            def events():
                for item in lineage_bound(auth, a2a_server.stream(
                        engine, agent, body, auth=auth, actor=actor, traceparent=traceparent)):
                    yield f"data: {json.dumps(item, default=str)}\n\n"

            return StreamingResponse(events(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache",
                                              "X-Accel-Buffering": "no"})

        def call() -> dict:
            if auth is not None:
                with lineage.acting(auth):
                    return a2a_server.handle(engine, agent, body, auth=auth, actor=actor,
                                             traceparent=traceparent)
            return a2a_server.handle(engine, agent, body, auth=auth, actor=actor,
                                     traceparent=traceparent)

        return JSONResponse(await run_in_threadpool(call))

    @router.put("/v1/agents/{role_key}/deployments/{environment}/a2a")
    @permit(P.AGENT_DEPLOY, scope=Scope.project(body="project_id"))
    def set_a2a(role_key: str, environment: str, body: ExposureBody) -> dict[str, Any]:
        """Turn A2A on or off for a deployment (off by default; on refused for agents with write
        tools until strong isolation lands, and for agents without a signing key — D11)."""
        from boss_engine.services.agent_deployments import DeploymentError

        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor,
                                        "workflow_manage")
            try:
                return a2a_server.set_exposure(connection, body.project_id, role_key,
                                               environment, enabled=body.enabled, actor=actor)
            except DeploymentError as error:
                raise HTTPException(status_code=error.status, detail=str(error)) from error

    return router


def lineage_bound(auth: Any, iterator: Any):
    """Drive a generator with the caller's identity acting (each step may run on another
    thread under a streaming response, so the context is re-entered per item)."""
    from boss_engine.identity import lineage

    while True:
        try:
            if auth is not None:
                with lineage.acting(auth):
                    item = next(iterator)
            else:
                item = next(iterator)
        except StopIteration:
            return
        yield item


__all__ = ["build_a2a_router"]
