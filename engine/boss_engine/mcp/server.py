"""MCP server exposing the BOSS engine tool layer to external agents.

This is how a GitHub Copilot agent, Claude, Cursor, or any MCP client becomes
"the BOSS Agent": it speaks MCP to this server, whose tools are exactly the
engine's tool layer (`boss_engine.tools`). Same tools the engine-native agent
uses — one source of truth.

The protocol logic here is **framework-agnostic** (pure dict in / dict out), so
the pure OSS engine keeps its "no HTTP framework" property. Two transports:

  * `serve_stdio()`  — the standard MCP transport, stdlib only (recommended;
    this is what Claude/Copilot MCP clients launch).
  * `asgi_app()`     — an optional HTTP/JSON-RPC transport that lazily imports
    FastAPI, matching the house style of the servers in `tools/`. Only import
    this if you actually want HTTP; it is never required by the engine.

Implements MCP JSON-RPC 2.0: initialize, notifications/initialized, tools/list,
tools/call.
"""
# NOTE: deliberately NO `from __future__ import annotations` here. `asgi_app()`
# lazily imports FastAPI's `Request` inside the function; with postponed
# annotations FastAPI would see the string "Request", fail to resolve it in
# module globals, silently treat the parameter as a query param, and 422 every
# JSON-RPC POST. Evaluating annotations at def-time keeps the lazy import valid.
import json
import hmac
import sys
from typing import Any, Optional

from .. import __version__
from ..tools import ToolRegistry, build_default_registry

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "boss-engine", "version": __version__}

# Shown to the client model at initialize (MCP `instructions`). This is what
# keeps external agents (Copilot/Claude/Cursor) on BOSS's governed workflow
# instead of improvising: kickoffs go through the PRD meeting, not straight to
# code, and artifacts (agendas/minutes/PRDs) are produced BY BOSS, not by the
# client writing files.
SERVER_INSTRUCTIONS = (
    "BOSS is a governed AI-labor control plane. Work flows: idea -> "
    "request_meeting (agent panel debates, produces minutes + PRD inside BOSS) "
    "-> human PRD approval -> code task auto-starts -> tested, gated PR. "
    "Rules for you as the client agent: (1) To kick off ANY new product/feature "
    "idea — including requests phrased as 'build X' — call request_meeting with "
    "the directive; do not call create_code_task first, and do not author "
    "agenda/minutes/PRD documents yourself: BOSS creates those and they appear "
    "in the console's Meetings area. (2) Use create_code_task only for a "
    "direct, already-specified engineering change that needs no PRD. (3) Use "
    "create_discovery when the idea first needs web research/evidence. (4) "
    "Everything you start is inspectable: list_meetings, list_code_tasks, "
    "get_code_task, project_status."
)


class MCPServer:
    """Bridges MCP JSON-RPC calls onto the engine tool registry.

    project_id/actor are fixed for the session (the transport authenticates the
    caller). `read_only=True` hides write tools entirely — useful for exposing a
    safe, inspection-only engine view.
    """

    def __init__(
        self,
        ctx: Any,
        *,
        project_id: str,
        actor: str = "mcp",
        registry: Optional[ToolRegistry] = None,
        read_only: bool = False,
        http_token: Optional[str] = None,
    ):
        self.ctx = ctx
        self.project_id = project_id
        self.actor = actor
        self.registry = registry or build_default_registry()
        self.http_token = http_token
        # Catalog-only contexts have no durable workspace in which to record a
        # receipt, approval, lease, or task. Enforce read-only at the transport
        # boundary as well as at invocation time so every MCP host advertises an
        # honest capability set.
        self.read_only = read_only or not getattr(ctx, "durable_workspace_configured", True)

    # ---- schema ----------------------------------------------------------
    def _tools(self) -> list[dict]:
        kind = "read" if self.read_only else None
        tools = self.registry.mcp_schema(kind=kind)
        # These fields are transport/session authority, never model arguments.
        for tool in tools:
            schema = tool.get("inputSchema") or {}
            properties = dict(schema.get("properties") or {})
            for reserved in ("project_id", "_operator_confirmed", "_writes_allowed"):
                properties.pop(reserved, None)
            tool["inputSchema"] = {
                **schema,
                "properties": properties,
                "required": [
                    field for field in schema.get("required", []) if field in properties
                ],
            }
        return tools

    def _tool_allowed(self, name: str) -> bool:
        if not self.read_only:
            return True
        try:
            return self.registry.get(name).kind == "read"
        except Exception:
            return False

    # ---- JSON-RPC dispatch ----------------------------------------------
    def handle(self, request: dict) -> Optional[dict]:
        """Handle one JSON-RPC request. Returns a response dict, or None for
        notifications (which take no reply)."""
        if not isinstance(request, dict):
            return _err(None, -32600, "invalid JSON-RPC request")
        method = request.get("method")
        rid = request.get("id")

        if method == "initialize":
            return _ok(rid, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
                "instructions": SERVER_INSTRUCTIONS,
            })
        if method in ("notifications/initialized", "initialized"):
            return None  # notification: no response
        if method == "ping":
            return _ok(rid, {})
        if method == "tools/list":
            return _ok(rid, {"tools": self._tools()})
        if method == "tools/call":
            params = request.get("params") or {}
            if not isinstance(params, dict):
                return _err(rid, -32602, "tools/call params must be an object")
            return self._tools_call(rid, params)
        return _err(rid, -32601, f"method not found: {method}")

    def _tools_call(self, rid: Any, params: dict) -> dict:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return _err(rid, -32602, "tools/call requires a tool name")
        supplied_args = params.get("arguments") or {}
        if not isinstance(supplied_args, dict):
            return _err(rid, -32602, "tool arguments must be an object")
        # Session project_id is authoritative — never let tool arguments override
        # into another tenant (write tools historically honored args.project_id).
        args = dict(supplied_args)
        for reserved in ("project_id", "_operator_confirmed", "_writes_allowed"):
            args.pop(reserved, None)
        if not self._tool_allowed(name):
            return _ok(rid, _tool_result(
                {"ok": False, "error": f"tool '{name}' is not available (read-only mode)"},
                is_error=True,
            ))
        try:
            result = self.registry.invoke(
                name, self.ctx, project_id=self.project_id, actor=self.actor, args=args
            )
        except Exception as exc:  # unknown tool etc.
            return _ok(rid, _tool_result(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, is_error=True
            ))
        return _ok(rid, _tool_result(result, is_error=not result.get("ok", True)))

    # ---- transports ------------------------------------------------------
    def serve_stdio(self, *, stdin=None, stdout=None) -> None:
        """Standard MCP stdio transport: one JSON-RPC message per line on stdin,
        one response line per request on stdout. Stdlib only."""
        rin = stdin or sys.stdin
        rout = stdout or sys.stdout
        for line in rin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                _write(rout, _err(None, -32700, "parse error"))
                continue
            response = self.handle(request)
            if response is not None:
                _write(rout, response)

    def asgi_app(self):
        """Optional HTTP/JSON-RPC transport (POST /mcp, GET /health) plus the
        community Mission Control page (GET /). Lazily imports FastAPI so the
        pure engine never depends on it."""
        from fastapi import FastAPI, Request  # noqa: PLC0415 (intentionally lazy)
        from fastapi import HTTPException  # noqa: PLC0415
        from fastapi.responses import HTMLResponse  # noqa: PLC0415

        from .ui import mission_control_html  # noqa: PLC0415

        if not self.read_only and not self.http_token:
            raise ValueError("write-capable HTTP MCP requires BOSS_MCP_HTTP_TOKEN")

        app = FastAPI(title="boss-engine-mcp")
        server = self

        def require_token(request: Request) -> None:
            if not server.http_token:
                return
            authorization = request.headers.get("authorization", "")
            scheme, _, candidate = authorization.partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(
                candidate, server.http_token
            ):
                raise HTTPException(
                    status_code=401,
                    detail="MCP bearer token required",
                    headers={"WWW-Authenticate": "Bearer"},
                )

        @app.post("/mcp")
        async def mcp(request: Request):  # type: ignore[no-redef]
            require_token(request)
            try:
                body = await request.json()
            except json.JSONDecodeError:
                return _err(None, -32700, "parse error")
            resp = server.handle(body)
            return resp if resp is not None else {"jsonrpc": "2.0", "result": {}}

        @app.get("/health")
        async def health(request: Request):  # type: ignore[no-redef]
            require_token(request)
            return {"status": "ok", "server": SERVER_INFO["name"],
                    "tools": [t["name"] for t in server._tools()]}

        @app.get("/", response_class=HTMLResponse)
        async def mission_control():  # type: ignore[no-redef]
            # Read-only by construction: the page only ever calls read tools
            # through the same /mcp endpoint (see mcp/ui.py).
            return mission_control_html()

        return app


# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------
def _ok(rid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _tool_result(payload: dict, *, is_error: bool = False) -> dict:
    """MCP tools/call result: content blocks + isError flag."""
    return {
        "content": [{"type": "text", "text": json.dumps(payload, default=str)}],
        "isError": bool(is_error),
    }


def _write(stream, obj: dict) -> None:
    stream.write(json.dumps(obj) + "\n")
    stream.flush()
