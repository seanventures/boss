"""tools.register: an extension's tools are MCP tools, served by BOSS and called over the broker.

HOW THEY RIDE THE ONE POLICY / RECEIPT / AUDIT PATH. Enabling the extension in a project registers
one `mcp_servers` row there (registry._set_project → mcp_registry.register_mcp_server — the public
seam every MCP server uses) whose URL is this endpoint:

    POST /v1/ext/{extension_id}/projects/{project_id}/mcp      plain JSON-RPC MCP (no sessions)

From then on nothing about an extension tool is special: the agent loop lists it with
MCPClient.list_tools, offers it only if tool_policy says it is not Off, holds an Ask tool for a
person, and every call goes through mcp_registry.call_tool_with_receipt — the review rule, the
operator's Allow/Ask/Off, the approved-arguments check, the effect receipt, the tool_call_audit
row and the injection screen — before MCPClient posts here. A changed tool definition (a new
version) is quarantined to Ask like any MCP tool's. This module only answers the protocol:

    initialize / notifications/initialized / ping
    tools/list   from the manifest (riskTier as the MCP annotation risk_tier, which may raise the
                 tier BOSS applies and never lower it)
    tools/call   one broker RPC `tool.call` (timeout = spec.runtime.timeoutSeconds), the result
                 as MCP content; an extension failure is an isError result, said in words

THE BEARER: the server row carries a per-(install, project) token (registry.mcp_token, written to
the secret store by register_mcp_server); this endpoint recomputes it and compares in constant time.
A request without it, for a project the extension is not enabled in, or for an untrusted install,
gets nothing.
"""
from __future__ import annotations

import hmac
import json
from typing import Any, Optional

from .registry import ExtensionRefused, InstalledExtension, load, mcp_token

PROTOCOL_VERSION = "2025-06-18"


def _error(rid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _result(rid: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def tool_list(ext: InstalledExtension) -> list[dict[str, Any]]:
    manifest = ext.manifest
    return [{"name": t.name, "description": t.description, "inputSchema": t.inputSchema,
             "annotations": {"risk_tier": t.riskTier, "boss_extension": ext.extension_id}}
            for t in manifest.spec.tools]


def authorized(ext: InstalledExtension, project_id: str, authorization: Optional[str]) -> bool:
    given = str(authorization or "")
    if not given.lower().startswith("bearer "):
        return False
    return hmac.compare_digest(given[7:].strip(), mcp_token(ext, project_id))


def handle(engine: Any, extension_id: str, project_id: str, authorization: Optional[str],
           payload: Any) -> tuple[int, Optional[dict[str, Any]]]:
    """(HTTP status, JSON body or None for a notification)."""
    from . import broker

    with engine.begin() as c:
        ext = load(c, extension_id)
    if ext is None or not authorized(ext, project_id, authorization):
        # One answer for "no such extension" and "wrong token": the token is the only key.
        return 401, {"error": "unauthorized"}
    rid = payload.get("id") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        return 400, _error(rid, -32600, "not a JSON-RPC 2.0 request")
    method = str(payload.get("method") or "")
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    if method.startswith("notifications/"):
        return 202, None
    try:
        manifest = ext.manifest                                    # the trust gate
    except ExtensionRefused as exc:
        return 200, _error(rid, -32001, str(exc))
    if not ext.enabled_in(project_id):
        return 200, _error(rid, -32001, f"{extension_id} is not enabled in this project")
    if not manifest.has("tools.register"):
        return 200, _error(rid, -32601, f"{extension_id} registers no tools")
    if method == "initialize":
        return 200, _result(rid, {
            "protocolVersion": str(params.get("protocolVersion") or PROTOCOL_VERSION),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": f"boss-extension:{extension_id}", "version": ext.version}})
    if method == "ping":
        return 200, _result(rid, {})
    if method == "tools/list":
        return 200, _result(rid, {"tools": tool_list(ext)})
    if method == "tools/call":
        name = str(params.get("name") or "")
        if manifest.tool(name) is None:
            return 200, _error(rid, -32602, f"{extension_id} has no tool {name}")
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        outcome = broker.call(engine, ext, "tool.call", {"name": name, "arguments": args},
                              project_id=project_id)
        if not outcome.ok:
            return 200, _result(rid, {"isError": True, "content": [
                {"type": "text", "text": f"[{outcome.code}] {outcome.error or 'the extension failed'}"}]})
        value = outcome.result
        body: dict[str, Any] = {"isError": False, "content": [
            {"type": "text", "text": value if isinstance(value, str) else json.dumps(value, default=str)}]}
        if isinstance(value, dict):
            body["structuredContent"] = value
        return 200, _result(rid, body)
    return 200, _error(rid, -32601, f"unknown method {method}")
