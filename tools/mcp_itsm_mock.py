"""KG P5b: the ITSM mock MCP server — past tickets/incidents for Historical Pattern and the
Compliance Analyst's citation, incl. INC-2088 (the cashier story: a $250.00 payment with $31.40
interest due should pay down $218.60; the bug paid the full $250.00 down instead). Zero config:
every fixture is hardcoded, in-process, no network. Run standalone:

    .venv/bin/python -m uvicorn tools.mcp_itsm_mock:app --port 8766

Implements: initialize, notifications/initialized, tools/list, tools/call.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request, Response

app = FastAPI(title="boss-itsm-mock")

INCIDENTS: dict[str, dict[str, Any]] = {
    "INC-2088": {
        "incident_id": "INC-2088",
        "title": "Payment misapplied to principal instead of interest-then-principal order",
        "summary": "A $250.00 payment with $31.40 interest due should have paid down $218.60 "
                   "of principal; the system paid the full $250.00 down instead, skipping the "
                   "interest-first allocation Reg Z's repayment-order rule requires.",
        "payment_amount": 250.00,
        "interest_due": 31.40,
        "correct_paydown": 218.60,
        "actual_paydown": 250.00,
        "status": "resolved",
        # What a real ticket's "affected component" field carries: the code the fix touched.
        "affected_components": ["LoanTransactionType.isRepaymentType", "LoanPaymentAllocationRule"],
    },
}


def _get_incident(args: dict) -> dict:
    incident_id = str(args.get("incident_id") or "").strip()
    incident = INCIDENTS.get(incident_id)
    if incident is None:
        return {"error": f"no incident '{incident_id}'", "known_incidents": list(INCIDENTS)}
    return dict(incident)


def _list_incidents(args: dict) -> dict:
    return {"incidents": [dict(v) for v in INCIDENTS.values()]}


TOOLS = [
    {
        "name": "get_incident",
        "description": "Get a past ITSM incident/ticket by id, incl. its payment/allocation detail.",
        "inputSchema": {"type": "object",
                        "properties": {"incident_id": {"type": "string"}},
                        "required": ["incident_id"]},
    },
    {
        "name": "list_incidents",
        "description": "List every past ITSM incident this mock knows about.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]
TOOL_FNS = {"get_incident": _get_incident, "list_incidents": _list_incidents}


@app.post("/mcp")
async def mcp(request: Request):
    body = await request.json()
    method = body.get("method")
    rid = body.get("id")
    params = body.get("params") or {}

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                           "serverInfo": {"name": "boss-itsm-mock", "version": "0.1.0"}}}
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
    return {"status": "ok", "server": "boss-itsm-mock", "tools": [t["name"] for t in TOOLS]}
