"""Reliability MCP tools stay named and route to BOSS APIs (no live vendors)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "reliability_mcp_server.py"


def _load():
    spec = importlib.util.spec_from_file_location("reliability_mcp_server", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mcp = _load()
TOOLS = mcp.TOOLS


def test_tools_include_capabilities_verify_and_page():
    names = {tool["name"] for tool in TOOLS}
    assert names >= {
        "list_incidents",
        "get_investigation",
        "diagnose_incident",
        "list_capabilities",
        "verify_incident",
        "page_incident",
    }


def test_new_tools_declare_required_incident_id():
    by_name = {tool["name"]: tool for tool in TOOLS}
    assert "incident_id" in by_name["verify_incident"]["inputSchema"]["required"]
    assert "incident_id" in by_name["page_incident"]["inputSchema"]["required"]
    assert by_name["list_capabilities"]["inputSchema"]["type"] == "object"


def test_call_tool_routes_without_live_vendor_calls(monkeypatch):
    seen: list[tuple[str, str]] = []

    def fake_api(method, path, body=None):
        seen.append((method, path))
        return {"ok": True, "path": path, "body": body}

    monkeypatch.setattr(mcp, "_api", fake_api)
    assert mcp._call_tool("list_capabilities", {})["path"] == "/v1/ai-sre/capabilities"
    verify = mcp._call_tool("verify_incident", {"incident_id": "inc_1"})
    assert verify["path"] == "/v1/reliability/incidents/inc_1/verify"
    assert verify["body"]["project_id"]
    page = mcp._call_tool("page_incident", {"incident_id": "inc_1"})
    assert page["path"] == "/v1/oncall-sre/incidents/inc_1/page"
    assert seen == [
        ("GET", "/v1/ai-sre/capabilities"),
        ("POST", "/v1/reliability/incidents/inc_1/verify"),
        ("POST", "/v1/oncall-sre/incidents/inc_1/page"),
    ]
