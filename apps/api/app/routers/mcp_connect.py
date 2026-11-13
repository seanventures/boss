"""CONNECTORS, STDIO SERVERS AND GATEWAYS (BOSSAPP-849..851): the HTTP surface.

GET  /v1/mcp/connectors                     the connector catalog (search, category, pre-fill)
GET  /v1/mcp/stdio-templates                the project's stdio launch templates
POST /v1/mcp/stdio-templates                create one              (host:exec — Owner/Admin, step-up)
PATCH  /v1/mcp/stdio-templates/{id}         edit one                (host:exec)
DELETE /v1/mcp/stdio-templates/{id}         remove one              (host:exec; refused while used)
GET  /v1/mcp/stdio-instances                what this API process is running, with health
GET  /v1/mcp/gateways                       the project's gateways and their tokens (never a token)
POST /v1/mcp/gateways                       create one; its first token is in the answer ONCE
POST /v1/mcp/gateways/{id}/tokens           mint another token (shown once)
POST /v1/mcp/gateways/{id}/tokens/{tid}/revoke
POST /v1/mcp/gateways/{id}/revoke           revoke the gateway and every token
POST /mcp/gw/{id}                           THE GATEWAY: MCP streamable HTTP for outside clients,
                                            authenticated by that gateway's own token

WHO MAY DO WHAT. A stdio template is a command BOSS runs on its host, so writing one is
`host:exec` (Owner/Admin, step-up, critical, not delegable) — the strictest project permission for
that act. A gateway hands tools to a client outside BOSS, so creating one, minting or revoking a
token is `mcp_server:review` (Owner/Admin, step-up), the bar for exposing a server at all. Reads
are `mcp_server:read`. The gateway itself is `public:mcp_gateway_token`: no BOSS identity, only
the gateway's hashed, expiring, revocable token; every call is governed inside.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from boss_engine.db import begin
from boss_engine.identity import P, PublicReason, Scope, permit, public
from boss_engine.services import mcp_curated_gateways as gateways

_MAX_GATEWAY_BODY = 2 * 1024 * 1024


class _Body(BaseModel):
    project_id: str
    actor: Optional[str] = None


class TemplateBody(_Body):
    name: str
    image: str
    argv: list[str]
    description: Optional[str] = None
    network: str = "none"
    env_allowlist: list[str] = Field(default_factory=list)
    env_defaults: dict[str, str] = Field(default_factory=dict)
    env_from_auth: Optional[str] = None
    idle_ttl_seconds: int = 600
    reuse_key: str = "credential"
    connector_key: Optional[str] = None


class TemplatePatch(_Body):
    changes: dict[str, Any] = Field(default_factory=dict)


class GatewayBody(_Body):
    name: str
    profile: dict[str, Any]
    allowed_actions: Optional[list[str]] = None
    expires_in_days: Optional[int] = None


class TokenBody(_Body):
    expires_in_days: Optional[int] = None
    label: Optional[str] = None


def build_mcp_connect_router(
    *,
    engine,
    settings: Callable[[], Any],
    require_write_actor: Callable[..., str],
    require_reader: Callable[..., None],
) -> APIRouter:
    from boss_engine.services import connector_catalog, mcp_stdio_supervisor
    from boss_engine.services import mcp_stdio_templates as templates

    router = APIRouter(tags=["mcp-connect"])

    def _writer(connection, body: _Body) -> str:
        return require_write_actor(connection, body.project_id, body.actor, "pack_manage")

    def _template_fail(error: Exception) -> None:
        if isinstance(error, LookupError):
            raise HTTPException(status_code=404, detail=str(error)) from error
        if isinstance(error, (templates.StdioTemplateConflict, templates.StdioTemplateInUse)):
            raise HTTPException(status_code=409, detail=str(error)) from error
        if isinstance(error, templates.StdioTemplateError):
            raise HTTPException(status_code=422, detail=str(error)) from error
        raise error

    def _gateway_fail(error: Exception) -> None:
        if isinstance(error, LookupError):
            raise HTTPException(status_code=404, detail=str(error)) from error
        if isinstance(error, gateways.GatewayConflict):
            raise HTTPException(status_code=409, detail=str(error)) from error
        if isinstance(error, gateways.GatewayError):
            raise HTTPException(status_code=422, detail=str(error)) from error
        raise error

    # -------------------------------------------------------------------- the catalog (849)
    @router.get("/v1/mcp/connectors")
    @permit(P.MCP_SERVER_READ, scope=Scope.project_or_org(query="project_id"))
    def connectors(project_id: Optional[str] = None, q: str = "",
                   category: str = "") -> dict[str, Any]:
        added: set[str] = set()
        ready: set[str] = set()
        versions: dict[str, int] = {}
        if project_id:
            from boss_engine.services.mcp_registry import list_mcp_servers

            with begin(engine) as connection:
                require_reader(connection, project_id)
                added = {str(s["name"]) for s in list_mcp_servers(connection, project_id)}
                ready = {t["name"] for t in templates.list_templates(connection, project_id)
                         if t["status"] == "active"}
                versions = connector_catalog.published_versions(connection, project_id)
        return connector_catalog.catalog_view(
            settings(), project_id=project_id, query=q, category=category, added=added,
            templates=ready, versions=versions)

    # -------------------------------------------------------------- stdio templates (850)
    @router.get("/v1/mcp/stdio-templates")
    @permit(P.MCP_SERVER_READ, scope=Scope.project(query="project_id"))
    def list_stdio_templates(project_id: str) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            items = templates.list_templates(connection, project_id)
            for item in items:
                item["servers"] = templates.servers_using(connection, project_id, item["name"])
        return {"project_id": project_id, "items": items}

    @router.post("/v1/mcp/stdio-templates", status_code=201)
    @permit(P.HOST_EXEC, scope=Scope.project(body="project_id"))
    def create_stdio_template(body: TemplateBody) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = _writer(connection, body)
            try:
                return templates.create_template(
                    connection, body.project_id,
                    body.model_dump(exclude={"project_id", "actor"}), actor=actor)
            except Exception as error:  # noqa: BLE001
                _template_fail(error)
                raise

    @router.patch("/v1/mcp/stdio-templates/{template_id}")
    @permit(P.HOST_EXEC, scope=Scope.project(query="project_id"))
    def update_stdio_template(template_id: str, project_id: str,
                              body: TemplatePatch) -> dict[str, Any]:
        if body.project_id != project_id:
            raise HTTPException(status_code=422, detail="project_id differs from the query")
        with begin(engine) as connection:
            actor = _writer(connection, body)
            try:
                return templates.update_template(connection, project_id, template_id,
                                                 body.changes, actor=actor)
            except Exception as error:  # noqa: BLE001
                _template_fail(error)
                raise

    @router.delete("/v1/mcp/stdio-templates/{template_id}", status_code=204)
    @permit(P.HOST_EXEC, scope=Scope.project(query="project_id"))
    def delete_stdio_template(template_id: str, project_id: str,
                              actor: Optional[str] = None) -> Response:
        with begin(engine) as connection:
            who = require_write_actor(connection, project_id, actor, "pack_manage")
            try:
                templates.delete_template(connection, project_id, template_id, actor=who)
            except Exception as error:  # noqa: BLE001
                _template_fail(error)
                raise
        return Response(status_code=204)

    @router.get("/v1/mcp/stdio-instances")
    @permit(P.MCP_SERVER_READ, scope=Scope.project(query="project_id"))
    def stdio_instances(project_id: str) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
        return {"project_id": project_id,
                "items": mcp_stdio_supervisor.SUPERVISOR.health(project_id)}

    # ------------------------------------------------------------------- gateways (851)
    @router.get("/v1/mcp/gateways")
    @permit(P.MCP_SERVER_READ, scope=Scope.project(query="project_id"))
    def list_gateways(project_id: str) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            return {"project_id": project_id,
                    "items": gateways.list_gateways(connection, project_id)}

    @router.post("/v1/mcp/gateways", status_code=201)
    @permit(P.MCP_SERVER_REVIEW, scope=Scope.project(body="project_id"))
    def create_gateway(body: GatewayBody) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = _writer(connection, body)
            try:
                return gateways.create_gateway(
                    connection, body.project_id, name=body.name, profile=body.profile,
                    actor=actor, allowed_actions=body.allowed_actions,
                    expires_in_days=body.expires_in_days)
            except Exception as error:  # noqa: BLE001
                _gateway_fail(error)
                raise

    @router.post("/v1/mcp/gateways/{gateway_id}/tokens", status_code=201)
    @permit(P.MCP_SERVER_REVIEW, scope=Scope.project(body="project_id"))
    def mint_gateway_token(gateway_id: str, body: TokenBody) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = _writer(connection, body)
            try:
                return gateways.mint_token(connection, body.project_id, gateway_id, actor=actor,
                                           expires_in_days=body.expires_in_days,
                                           label=body.label)
            except Exception as error:  # noqa: BLE001
                _gateway_fail(error)
                raise

    @router.post("/v1/mcp/gateways/{gateway_id}/tokens/{token_id}/revoke")
    @permit(P.MCP_SERVER_REVIEW, scope=Scope.project(body="project_id"))
    def revoke_gateway_token(gateway_id: str, token_id: str, body: _Body) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = _writer(connection, body)
            try:
                return gateways.revoke_token(connection, body.project_id, gateway_id, token_id,
                                             actor=actor)
            except Exception as error:  # noqa: BLE001
                _gateway_fail(error)
                raise

    @router.post("/v1/mcp/gateways/{gateway_id}/revoke")
    @permit(P.MCP_SERVER_REVIEW, scope=Scope.project(body="project_id"))
    def revoke_gateway(gateway_id: str, body: _Body) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = _writer(connection, body)
            try:
                return gateways.revoke_gateway(connection, body.project_id, gateway_id,
                                               actor=actor)
            except Exception as error:  # noqa: BLE001
                _gateway_fail(error)
                raise

    # ------------------------------------------------------- the gateway itself (public)
    @router.post("/mcp/gw/{gateway_id}")
    @public(reason=PublicReason.MCP_GATEWAY_TOKEN, verifier=gateways.verify_request)
    async def gateway_rpc(gateway_id: str, request: Request) -> Response:
        """MCP streamable HTTP for an outside client. JSON answers (no server-initiated
        stream); a notification is 202. The token is checked before the body is read."""
        try:
            gateway = await run_in_threadpool(gateways.verify_request, engine, gateway_id,
                                              dict(request.headers))
        except gateways.GatewayAuthError as refused:
            return JSONResponse(status_code=401, content={"error": str(refused)},
                                headers={"WWW-Authenticate": 'Bearer realm="boss-mcp-gateway"'})
        raw = await request.body()
        if len(raw) > _MAX_GATEWAY_BODY:
            return JSONResponse(status_code=413, content={"error": "request too large"})
        try:
            message = json.loads(raw or b"null")
        except ValueError:
            return JSONResponse(status_code=400, content={
                "jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
        current = settings()

        def _answer() -> Any:
            if isinstance(message, list):
                return [a for a in (gateways.handle(engine, current, gateway, m)
                                    for m in message[:50]) if a is not None]
            return gateways.handle(engine, current, gateway, message)

        answer = await run_in_threadpool(_answer)
        if answer is None or answer == []:
            return Response(status_code=202)
        return JSONResponse(content=answer)

    return router
