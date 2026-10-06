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
import contextvars
import json
import hmac
import sys
from typing import Any, Callable, Mapping, Optional

from .. import __version__
from ..tools import ToolRegistry, build_default_registry

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "boss-engine", "version": __version__}
_UNSCOPED_SQL_TOOLS = frozenset({"query_db", "write_db"})

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


#: The identity the HTTP transport authenticated for the request being handled (an AuthContext).
_REQUEST_AUTH: contextvars.ContextVar[Any] = contextvars.ContextVar("boss_mcp_request_auth", default=None)
#: An HTTP request the transport admitted WITHOUT an identity (the legacy shared token): its tool
#: calls carry none, and never fall back to the stdio session identity.
_NO_IDENTITY = object()


def _is_boss_credential(token: str) -> bool:
    """A credential BOSS identity verifies itself: an API key (boss_pat_/boss_sak_) or a run token."""
    from ..identity.authn import parse_api_key

    return parse_api_key(token) is not None or (token.startswith("eyJ") and token.count(".") == 2)


def authenticate_transport(headers: Mapping[str, str], *, method: str = "POST", path: str = "/mcp",
                           client_ip: Optional[str] = None) -> Any:
    """The verified identity of an MCP caller, from what its TRANSPORT presented (an
    `Authorization` bearer: a BOSS API key, or a DPoP-bound run token with its `DPoP` proof).
    None when it presented no BOSS credential or one that does not verify — the identity gate
    then refuses the call under enforce. Never read from a tool argument; never the legacy
    network/shared-key bridges (an MCP session is not a console request)."""
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    scheme, _, token = lowered.get("authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() not in ("bearer", "dpop") or not token or not _is_boss_credential(token):
        return None
    from ..identity import authn

    creds = authn.RequestCredentials(method=method, path=path, headers=lowered, cookies={},
                                     client_ip=client_ip, peer_ip=client_ip)
    try:
        return authn.authenticate(creds)
    except Exception:  # noqa: BLE001 — an unverifiable credential is no identity (fail closed)
        return None


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
        api_surface: Any = None,
        api_domains: Optional[set[str]] = None,
        identity: Optional[Callable[[], Any]] = None,
    ):
        """`identity` returns the verified caller identity for one call (stdio: the session's
        BOSS API key, re-checked live on every call). The HTTP transport authenticates each
        request's bearer instead."""
        self.ctx = ctx
        self.identity = identity
        self.project_id = project_id
        self.actor = actor
        self.registry = registry or build_default_registry()
        self.http_token = http_token
        # In API-backed mode every call must use the scoped API credential.
        # Native reads as well as writes could otherwise use raw DATABASE_URL
        # under actor "mcp" and bypass that member's API permissions.
        self.api_only = api_surface is not None
        self.api_tool_names: set[str] = set()
        self.read_only = read_only
        self.catalog_only = not getattr(ctx, "durable_workspace_configured", True)
        if api_surface is not None:
            pinned = getattr(api_surface, "pinned_project_id", None)
            if pinned is not None and pinned != project_id:
                raise ValueError("MCP API project must match the pinned engine project")
            from .api_surface import register_api_tools

            existing = set(self.registry.names())
            register_api_tools(
                self.registry, api_surface, read_only=self.read_only,
                domains=api_domains,
            )
            self.api_tool_names = set(self.registry.names()) - existing

    # ---- schema ----------------------------------------------------------
    def _tools(self) -> list[dict]:
        tools = [spec.mcp() for spec in self.registry.specs() if (
            spec.name not in _UNSCOPED_SQL_TOOLS
            and
            (not self.api_only or spec.name in self.api_tool_names)
            and
            (not self.read_only or spec.kind == "read")
            and (not self.catalog_only or spec.kind == "read"
                 or not spec.requires_durable_workspace)
        )]
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
        try:
            spec = self.registry.get(name)
            return (spec.name not in _UNSCOPED_SQL_TOOLS
                    and (not self.api_only or spec.name in self.api_tool_names)
                    and (not self.read_only or spec.kind == "read")
                    and (not self.catalog_only or spec.kind == "read"
                         or not spec.requires_durable_workspace))
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
            reason = ("read-only mode" if self.read_only else
                      "API-backed sessions require API-authorized operations" if self.api_only
                      else "durable workspace required")
            return _ok(rid, _tool_result(
                {"ok": False, "error": f"tool '{name}' is not available ({reason})"},
                is_error=True,
            ))
        # WHO IS CALLING: the identity the transport authenticated, never a tool argument. Every
        # tool call is decided as that caller (the registry's identity gate; fail closed under
        # enforce when there is none), and jobs it enqueues carry it (R8).
        auth = self._caller()
        from ..tools.registry import CURRENT_TOOL_AUTH

        tool_token = CURRENT_TOOL_AUTH.set(auth)
        try:
            if auth is not None:
                from ..identity import lineage

                with lineage.acting(auth):
                    result = self.registry.invoke(
                        name, self.ctx, project_id=self.project_id, actor=self.actor, args=args, auth=auth)
            else:
                result = self.registry.invoke(
                    name, self.ctx, project_id=self.project_id, actor=self.actor, args=args, auth=None)
        except Exception as exc:  # unknown tool etc.
            return _ok(rid, _tool_result(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, is_error=True
            ))
        finally:
            CURRENT_TOOL_AUTH.reset(tool_token)
        return _ok(rid, _tool_result(result, is_error=not result.get("ok", True)))

    def _caller(self) -> Any:
        """The verified identity for this call: the HTTP request's, else the session's."""
        request_auth = _REQUEST_AUTH.get()
        if request_auth is _NO_IDENTITY:
            return None
        if request_auth is not None:
            return request_auth
        if self.identity is None:
            return None
        try:
            return self.identity()
        except Exception:  # noqa: BLE001 — a credential that no longer verifies is no identity
            return None

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

        async def admitted(request: Request) -> Any:
            """Admit the request and return its verified identity. The legacy shared
            BOSS_MCP_HTTP_TOKEN admits but identifies nobody (None), so its tool calls are refused
            under enforce. A BOSS API key or run token is verified by identity, live; one that does
            not verify is refused, never downgraded to anonymous."""
            authorization = request.headers.get("authorization", "")
            scheme, _, candidate = authorization.partition(" ")
            candidate = candidate.strip()
            if server.http_token and scheme.lower() == "bearer" and hmac.compare_digest(
                candidate.encode(), server.http_token.encode()
            ):
                return None
            if candidate and _is_boss_credential(candidate):
                from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

                found = await run_in_threadpool(
                    authenticate_transport, dict(request.headers), method=request.method,
                    path=request.url.path, client_ip=request.client.host if request.client else None)
                if found is not None:
                    return found
            elif not server.http_token:
                return None
            raise HTTPException(
                status_code=401,
                detail="MCP bearer token required",
                headers={"WWW-Authenticate": "Bearer"},
            )

        @app.post("/mcp")
        async def mcp(request: Request):  # type: ignore[no-redef]
            auth = await admitted(request)
            try:
                body = await request.json()
            except json.JSONDecodeError:
                return _err(None, -32700, "parse error")
            token = _REQUEST_AUTH.set(auth if auth is not None else _NO_IDENTITY)
            try:
                resp = server.handle(body)
            finally:
                _REQUEST_AUTH.reset(token)
            return resp if resp is not None else {"jsonrpc": "2.0", "result": {}}

        @app.get("/health")
        async def health(request: Request):  # type: ignore[no-redef]
            await admitted(request)
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
