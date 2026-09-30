#!/usr/bin/env python3
"""RB-20 — the investigation MCP server: expose BOSS AI-SRE investigations to any MCP client
(Claude Code, Cursor, …) so an engineer's coding agent pulls full incident context in one tool call.

Dependency-free stdio MCP server (JSON-RPC 2.0 over stdin/stdout) backed by the BOSS API.

Config (env):
    BOSS_API_URL       e.g. https://boss.example.com
    BOSS_API_KEY       console/API key (Authorization: Bearer)
    BOSS_PROJECT_ID    default project (default: boss-dogfood)

Claude Code registration:
    claude mcp add boss-sre -- python3 scripts/reliability_mcp_server.py
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.parse
import urllib.request

API = os.environ.get("BOSS_API_URL", "http://localhost:8000").rstrip("/")
KEY = os.environ.get("BOSS_API_KEY", "")
PROJECT = os.environ.get("BOSS_PROJECT_ID", "boss-dogfood")
CTX = ssl._create_unverified_context()  # self-hosted certs on the tailnet

TOOLS = [
    {"name": "list_incidents",
     "description": "List reliability incidents on the board (optionally filter by status: "
                    "open|triaged|diagnosing|remediating|resolved|dismissed).",
     "inputSchema": {"type": "object", "properties": {
         "status": {"type": "string"}, "limit": {"type": "integer", "default": 20}}}},
    {"name": "get_investigation",
     "description": "Full investigation bundle for one incident: signals, evidence (ranked "
                    "root-cause hypotheses, context, verification), remediations (fix PRs), the "
                    "service dependency graph, and the current autonomy decision.",
     "inputSchema": {"type": "object", "properties": {
         "incident_id": {"type": "string"}}, "required": ["incident_id"]}},
    {"name": "diagnose_incident",
     "description": "Run the autonomous investigation engine on an incident now (parallel "
                    "hypotheses ranked by confidence).",
     "inputSchema": {"type": "object", "properties": {
         "incident_id": {"type": "string"}}, "required": ["incident_id"]}},
    {"name": "list_capabilities",
     "description": "Honest AI SRE capability ledger (awesome-ai-sre categories mapped onto "
                    "BOSS surfaces). Missing stays missing. Never invents coverage.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "verify_incident",
     "description": "Re-watch an incident's signals: recurrence reopens it, a clear stability "
                    "window can resolve it. Posts to BOSS only — no live vendor calls.",
     "inputSchema": {"type": "object", "properties": {
         "incident_id": {"type": "string"},
         "source": {"type": "object"},
         "window": {"type": "object"},
         "stability_minutes": {"type": "number", "default": 30},
         "query": {"type": "string"}},
         "required": ["incident_id"]}},
    {"name": "page_incident",
     "description": "Open a receipted on-call page for a reliability incident. Adapters "
                    "dry-run unless credentials are configured (never calls vendors from MCP).",
     "inputSchema": {"type": "object", "properties": {
         "incident_id": {"type": "string"},
         "title": {"type": "string"},
         "severity": {"type": "string"},
         "service": {"type": "string"},
         "adapter": {"type": "string", "default": "slack_webhook"}},
         "required": ["incident_id"]}},
]


def _api(method: str, path: str, body=None):
    url = f"{API}{path}"
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {KEY}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
        return json.loads(r.read().decode())


def _call_tool(name: str, args: dict):
    if name == "list_incidents":
        q = {"project_id": PROJECT, "limit": args.get("limit", 20)}
        if args.get("status"):
            q["status"] = args["status"]
        return _api("GET", f"/v1/reliability/incidents?{urllib.parse.urlencode(q)}")
    if name == "get_investigation":
        return _api("GET", f"/v1/reliability/incidents/{args['incident_id']}/investigation"
                           f"?project_id={PROJECT}")
    if name == "diagnose_incident":
        return _api("POST", f"/v1/reliability/incidents/{args['incident_id']}/diagnose",
                    {"project_id": PROJECT, "actor": "ceo"})
    if name == "list_capabilities":
        return _api("GET", "/v1/ai-sre/capabilities")
    if name == "verify_incident":
        return _api("POST", f"/v1/reliability/incidents/{args['incident_id']}/verify", {
            "project_id": PROJECT,
            "actor": args.get("actor", "ceo"),
            "source": args.get("source") or {"kind": "memory"},
            "window": args.get("window") or {"start": 0, "end": 9e9},
            "query": args.get("query", ""),
            "stability_minutes": args.get("stability_minutes", 30.0),
        })
    if name == "page_incident":
        return _api("POST", f"/v1/oncall-sre/incidents/{args['incident_id']}/page", {
            "project_id": PROJECT,
            "actor": args.get("actor", "ceo"),
            "title": args.get("title", ""),
            "severity": args.get("severity", "warning"),
            "service": args.get("service", ""),
            "adapter": args.get("adapter", "slack_webhook"),
        })
    raise ValueError(f"unknown tool {name}")


def _reply(msg_id, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = {"code": -32000, "message": str(error)}
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, msg_id = msg.get("method", ""), msg.get("id")
        if method == "initialize":
            _reply(msg_id, {"protocolVersion": msg.get("params", {}).get(
                                "protocolVersion", "2025-06-18"),
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "boss-sre", "version": "1.0.0"}})
        elif method == "notifications/initialized":
            continue                                  # notification: no reply
        elif method == "tools/list":
            _reply(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            params = msg.get("params", {})
            try:
                result = _call_tool(params.get("name", ""), params.get("arguments") or {})
                _reply(msg_id, {"content": [{"type": "text",
                                             "text": json.dumps(result, default=str)}]})
            except Exception as exc:  # tool errors surface to the client, server keeps running
                _reply(msg_id, {"content": [{"type": "text", "text": f"error: {exc}"}],
                                "isError": True})
        elif msg_id is not None:
            _reply(msg_id, error=f"method not supported: {method}")


if __name__ == "__main__":
    main()
