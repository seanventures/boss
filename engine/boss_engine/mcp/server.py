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
from __future__ import annotations

import json
import sys
from typing import Any, Optional

from .. import __version__
from ..tools import ToolRegistry, build_default_registry

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "boss-engine", "version": __version__}


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
    ):
        self.ctx = ctx
        self.project_id = project_id
        self.actor = actor
        self.registry = registry or build_default_registry()
        self.read_only = read_only

    # ---- schema ----------------------------------------------------------
    def _tools(self) -> list[dict]:
        kind = "read" if self.read_only else None
        return self.registry.mcp_schema(kind=kind)

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
        method = request.get("method")
        rid = request.get("id")

        if method == "initialize":
            return _ok(rid, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            })
        if method in ("notifications/initialized", "initialized"):
            return None  # notification: no response
        if method == "ping":
            return _ok(rid, {})
        if method == "tools/list":
            return _ok(rid, {"tools": self._tools()})
        if method == "tools/call":
            return self._tools_call(rid, request.get("params") or {})
        return _err(rid, -32601, f"method not found: {method}")

    def _tools_call(self, rid: Any, params: dict) -> dict:
        name = params.get("name")
        args = params.get("arguments") or {}
        if not name:
            return _err(rid, -32602, "tools/call requires a tool name")
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
        """Optional HTTP/JSON-RPC transport (POST /mcp, GET /health). Lazily
        imports FastAPI so the pure engine never depends on it."""
        from fastapi import FastAPI, Request  # noqa: PLC0415 (intentionally lazy)

        app = FastAPI(title="boss-engine-mcp")
        server = self

        @app.post("/mcp")
        async def mcp(request: Request):  # type: ignore[no-redef]
            body = await request.json()
            resp = server.handle(body)
            return resp if resp is not None else {"jsonrpc": "2.0", "result": {}}

        @app.get("/health")
        async def health():  # type: ignore[no-redef]
            return {"status": "ok", "server": SERVER_INFO["name"],
                    "tools": [t["name"] for t in server._tools()]}

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
