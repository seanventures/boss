"""KG P5b: the Profile Service mock MCP server — a minimal, zero-config fixture so the
banking-regulatory sample's contract `calls` resolve. Every fixture is hardcoded, in-process,
no network. Run standalone:

    .venv/bin/python -m uvicorn tools.mcp_profile_service_mock:app --port 8768

Implements: initialize, notifications/initialized, tools/list, tools/call.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request, Response

app = FastAPI(title="boss-profile-service-mock")


def _get_customer_profile(args: dict) -> dict[str, Any]:
    customer_id = str(args.get("customer_id") or "").strip() or "unknown"
    return {"customer_id": customer_id, "segment": "retail", "risk_tier": "standard"}


TOOLS = [
    {
        "name": "get_customer_profile",
        "description": "Get a customer's profile (segment, risk tier) by id.",
        "inputSchema": {"type": "object", "properties": {"customer_id": {"type": "string"}},
                        "required": ["customer_id"]},
    },
]
TOOL_FNS: dict[str, Any] = {"get_customer_profile": _get_customer_profile}


@app.post("/mcp")
async def mcp(request: Request):
    body = await request.json()
    method = body.get("method")
    rid = body.get("id")
    params = body.get("params") or {}

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                           "serverInfo": {"name": "boss-profile-service-mock", "version": "0.1.0"}}}
    if method == "notifications/initialized":
        return Response(status_code=202)
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = TOOL_FNS.get(name)
        if fn is None:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32602, "message": f"unknown tool {name}"}}
        result = fn(args)
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"content": [{"type": "text", "text": json.dumps(result)}],
                           "isError": False}}
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


@app.get("/health")
def health():
    return {"status": "ok", "server": "boss-profile-service-mock", "tools": [t["name"] for t in TOOLS]}
