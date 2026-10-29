"""KG P5b: the DQ Monitor mock MCP server — a minimal, zero-config fixture so the banking-dq
sample's contract `calls` resolve. Run standalone:

    .venv/bin/python -m uvicorn tools.mcp_dq_monitor_mock:app --port 8767
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request, Response

app = FastAPI(title="boss-dq-monitor-mock")


def _check_data_quality(args: dict) -> dict:
    dataset = str(args.get("dataset") or "").strip() or "unknown"
    return {"dataset": dataset, "status": "clean", "issues": []}


TOOLS = [
    {
        "name": "check_data_quality",
        "description": "Get the last data-quality check result for a named dataset.",
        "inputSchema": {"type": "object", "properties": {"dataset": {"type": "string"}},
                        "required": ["dataset"]},
    },
]
TOOL_FNS: dict[str, Any] = {"check_data_quality": _check_data_quality}


@app.post("/mcp")
async def mcp(request: Request):
    body = await request.json()
    method = body.get("method")
    rid = body.get("id")
    params = body.get("params") or {}

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                           "serverInfo": {"name": "boss-dq-monitor-mock", "version": "0.1.0"}}}
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
    return {"status": "ok", "server": "boss-dq-monitor-mock", "tools": [t["name"] for t in TOOLS]}
