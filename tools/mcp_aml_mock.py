"""The AML case study's three mock MCP connectors — case management, KYC and transactions — over
the synthetic bank in tools/aml_sample_data.py. Sample tools, like the Service Desk mock: zero config,
in-process, no network, served by the `mcp-tools` service (tools/mcp_tools_mux.py) on one port,
one path per connector:

    /case-management/mcp   get_alert, list_alerts, add_case_note (the only write)
    /kyc/mcp               get_kyc_profile
    /transactions/mcp      query_transactions

Run standalone:  .venv/bin/python -m uvicorn tools.mcp_aml_mock:app --port 8773

Read-only except `add_case_note`, which appends to an in-memory case file (a restart forgets it;
the investigation's own record is the Knowledge Graph's Context Graph, not this mock).
Implements: initialize, notifications/initialized, tools/list, tools/call.
"""
from __future__ import annotations

import json
import threading
from typing import Any, Callable

from fastapi import FastAPI, Request, Response

from tools.aml_sample_data import generate, summarize

app = FastAPI(title="boss-aml-mock")

#: How many transactions one call returns at most; the summary always covers the whole window.
MAX_TRANSACTIONS = 500

_NOTES: dict[str, list[dict[str, Any]]] = {}
_NOTES_LOCK = threading.Lock()


def _all_alerts() -> dict[str, dict[str, Any]]:
    ds = generate()
    return {**ds.historical_alerts, **ds.alerts}


def _get_alert(args: dict) -> dict:
    alert_id = str(args.get("alert_id") or "").strip()
    alert = _all_alerts().get(alert_id)
    if alert is None:
        return {"error": f"no alert '{alert_id}'", "known_alerts": sorted(_all_alerts())}
    ds = generate()
    flagged = {t["transaction_id"]: t for t in ds.transactions
               if t["transaction_id"] in set(alert.get("flagged_transactions") or [])}
    return {**alert, "flagged": [flagged[i] for i in alert.get("flagged_transactions") or []
                                 if i in flagged],
            "notes": list(_NOTES.get(alert_id, []))}


def _list_alerts(args: dict) -> dict:
    customer_id = str(args.get("customer_id") or "").strip()
    account_id = str(args.get("account_id") or "").strip()
    status = str(args.get("status") or "").strip()
    out = []
    for alert in _all_alerts().values():
        if customer_id and alert.get("customer_id") != customer_id:
            continue
        if account_id and alert.get("account_id") != account_id:
            continue
        if status and alert.get("status") != status:
            continue
        out.append({k: v for k, v in alert.items() if k != "flagged_transactions"})
    return {"alerts": sorted(out, key=lambda a: a["alert_id"])}


def _add_case_note(args: dict) -> dict:
    alert_id = str(args.get("alert_id") or "").strip()
    note = str(args.get("note") or "").strip()
    if alert_id not in _all_alerts():
        return {"error": f"no alert '{alert_id}'"}
    if not note:
        return {"error": "a case note needs text"}
    with _NOTES_LOCK:
        notes = _NOTES.setdefault(alert_id, [])
        entry = {"note_id": f"NOTE-{alert_id}-{len(notes) + 1}", "alert_id": alert_id,
                 "author": str(args.get("author") or "boss"), "note": note[:4000]}
        notes.append(entry)
    return {"ok": True, **entry}


def _get_kyc_profile(args: dict) -> dict:
    customer_id = str(args.get("customer_id") or "").strip()
    ds = generate()
    profile = ds.kyc_profiles.get(customer_id)
    if profile is None:
        return {"error": f"no KYC profile for '{customer_id}'"}
    customer = ds.customers[customer_id]
    related = [{"party": b if a == customer_id else a, "reason": why}
               for a, b, why in ds.relationships if customer_id in (a, b)]
    return {**profile, "name": customer["name"], "segment": customer["segment"],
            "risk_rating": customer["risk_rating"], "onboarded": customer["onboarded"],
            "accounts": sorted(a for a, acc in ds.accounts.items()
                               if acc["customer_id"] == customer_id),
            "related_parties": related}


def _query_transactions(args: dict) -> dict:
    account_id = str(args.get("account_id") or "").strip()
    ds = generate()
    if account_id not in ds.accounts:
        return {"error": f"no account '{account_id}'"}
    rows = ds.account_transactions(account_id, str(args.get("from_date") or ""),
                                   str(args.get("to_date") or ""))
    try:
        limit = max(1, min(MAX_TRANSACTIONS, int(args.get("limit") or MAX_TRANSACTIONS)))
    except (TypeError, ValueError):
        limit = MAX_TRANSACTIONS
    return {"account_id": account_id, "summary": summarize(rows),
            "transactions": rows[-limit:], "truncated": len(rows) > limit}


_STR = {"type": "string"}
SERVERS: dict[str, dict[str, Any]] = {
    "case-management": {
        "tools": [
            {"name": "get_alert", "description": "One monitoring alert with its flagged "
             "transactions and case notes.",
             "inputSchema": {"type": "object", "properties": {"alert_id": _STR},
                             "required": ["alert_id"]}},
            {"name": "list_alerts", "description": "Alerts (open and closed), filtered by "
             "customer, account or status.",
             "inputSchema": {"type": "object", "properties": {
                 "customer_id": _STR, "account_id": _STR, "status": _STR}}},
            {"name": "add_case_note", "description": "Append a note to an alert's case file. "
             "The only write; it never closes an alert or files a report.",
             "inputSchema": {"type": "object", "properties": {
                 "alert_id": _STR, "note": _STR, "author": _STR},
                 "required": ["alert_id", "note"]},
             "annotations": {"risk_tier": 1}},
        ],
        "fns": {"get_alert": _get_alert, "list_alerts": _list_alerts,
                "add_case_note": _add_case_note},
    },
    "kyc": {
        "tools": [
            {"name": "get_kyc_profile", "description": "A customer's KYC profile: occupation, "
             "expected activity, source of funds, documents on file and related parties.",
             "inputSchema": {"type": "object", "properties": {"customer_id": _STR},
                             "required": ["customer_id"]}},
        ],
        "fns": {"get_kyc_profile": _get_kyc_profile},
    },
    "transactions": {
        "tools": [
            {"name": "query_transactions", "description": "An account's transactions in a date "
             "window (ISO dates), newest last, with a summary of the whole window.",
             "inputSchema": {"type": "object", "properties": {
                 "account_id": _STR, "from_date": _STR, "to_date": _STR,
                 "limit": {"type": "integer"}}, "required": ["account_id"]}},
        ],
        "fns": {"query_transactions": _query_transactions},
    },
}
#: {server: {tool: fn}} for in-process callers and tests.
TOOL_FNS: dict[str, dict[str, Callable[[dict], dict]]] = {
    name: spec["fns"] for name, spec in SERVERS.items()}


def _rpc(server: str, body: dict) -> Any:
    method = body.get("method")
    rid = body.get("id")
    params = body.get("params") or {}
    spec = SERVERS[server]
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                           "serverInfo": {"name": f"boss-aml-{server}-mock",
                                          "version": "0.1.0"}}}
    if method == "notifications/initialized":
        return Response(status_code=202)
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": spec["tools"]}}
    if method == "tools/call":
        fn = spec["fns"].get(params.get("name"))
        if fn is None:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32602, "message": f"unknown tool {params.get('name')}"}}
        result = fn(params.get("arguments") or {})
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"content": [{"type": "text", "text": json.dumps(result)}],
                           "isError": bool(result.get("error"))}}
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def _route(server: str):
    async def handler(request: Request):
        return _rpc(server, await request.json())
    return handler


for _name in SERVERS:
    app.add_api_route(f"/{_name}/mcp", _route(_name), methods=["POST"])


@app.get("/health")
def health():
    return {"status": "ok", "server": "boss-aml-mock",
            "connectors": {name: [t["name"] for t in spec["tools"]]
                           for name, spec in SERVERS.items()}}
