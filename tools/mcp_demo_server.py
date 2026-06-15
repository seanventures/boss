"""A minimal but protocol-faithful MCP server over HTTP (JSON-RPC 2.0).

Exposes external-knowledge tools that BOSS does NOT have internally — so an
agent can combine BOSS's own data (internal tools) with this server's data
(MCP tools) to ground a plan. Run standalone:

    .venv/bin/python -m uvicorn tools.mcp_demo_server:app --port 8765

Implements: initialize, notifications/initialized, tools/list, tools/call.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request, Response

app = FastAPI(title="boss-devops-reference-mcp")

# ---- Tool implementations (external reference data) ----


def _cloud_reference_pricing(args: dict) -> dict:
    provider = str(args.get("provider") or "digitalocean").lower()
    catalog: dict[str, Any] = {
        "digitalocean": {
            "currency": "USD/month",
            "droplets": {"s-1vcpu-1gb": 6, "s-2vcpu-2gb": 18, "s-4vcpu-8gb": 48, "s-8vcpu-16gb": 96},
            "managed_postgres": {"db-s-1vcpu-1gb": 15, "db-s-2vcpu-4gb": 60},
            "spaces_object_storage_per_tb": 20,
            "load_balancer": 12,
        },
        "aws": {
            "currency": "USD/month (on-demand, us-east-1, ~730h)",
            "ec2": {"t3.micro": 7.5, "t3.small": 15, "t3.medium": 30, "m5.large": 70},
            "rds_postgres": {"db.t3.micro": 12, "db.t3.small": 24},
            "s3_per_tb": 23,
            "alb": 16,
        },
    }
    data = catalog.get(provider)
    if not data:
        return {"error": f"no reference pricing for provider '{provider}'", "known_providers": list(catalog)}
    return {"provider": provider, "as_of": "2026-Q2 public list prices", **data}


def _engineering_runbook(args: dict) -> dict:
    topic = str(args.get("topic") or "").lower()
    book = {
        "database backups": [
            "Take automated daily logical + weekly physical backups to object storage.",
            "Encrypt at rest; restrict restore credentials; rotate keys.",
            "Run a monthly restore drill into an isolated sandbox and measure RTO/RPO.",
        ],
        "cost reduction": [
            "Right-size compute to p95 utilization; kill idle/orphaned resources.",
            "Move cold object data to cheaper storage tiers; add lifecycle policies.",
            "Commit to reserved/savings plans only after 30 days of stable usage.",
        ],
        "sso": [
            "Support SAML 2.0 + OIDC; map IdP groups to roles via SCIM.",
            "Roll out behind a flag: metadata-only -> auth-allowed -> provisioning-allowed.",
            "Enforce just-in-time provisioning and audit every assertion.",
        ],
    }
    for key, steps in book.items():
        if key in topic or topic in key:
            return {"topic": key, "best_practices": steps, "source": "DevOps reference (external)"}
    return {"topic": topic, "best_practices": [], "note": "no runbook for that topic", "available_topics": list(book)}


TOOLS = [
    {
        "name": "cloud_reference_pricing",
        "description": "Get external public-list-price reference pricing for a cloud provider (digitalocean or aws). Use to benchmark actual spend against market rates.",
        "inputSchema": {
            "type": "object",
            "properties": {"provider": {"type": "string", "description": "digitalocean or aws"}},
            "required": ["provider"],
        },
    },
    {
        "name": "engineering_runbook",
        "description": "Get external best-practice runbook steps for an engineering topic (e.g. 'database backups', 'cost reduction', 'sso').",
        "inputSchema": {
            "type": "object",
            "properties": {"topic": {"type": "string"}},
            "required": ["topic"],
        },
    },
]
TOOL_FNS = {"cloud_reference_pricing": _cloud_reference_pricing, "engineering_runbook": _engineering_runbook}


@app.post("/mcp")
async def mcp(request: Request):
    body = await request.json()
    method = body.get("method")
    rid = body.get("id")
    params = body.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "boss-devops-reference", "version": "0.1.0"},
            },
        }
    if method == "notifications/initialized":
        return Response(status_code=202)
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = TOOL_FNS.get(name)
        if fn is None:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"unknown tool {name}"}}
        result = fn(args)
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {"content": [{"type": "text", "text": json.dumps(result)}], "isError": False},
        }
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}}


@app.get("/health")
def health():
    return {"status": "ok", "server": "boss-devops-reference-mcp", "tools": [t["name"] for t in TOOLS]}
