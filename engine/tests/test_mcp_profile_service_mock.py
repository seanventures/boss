"""KG P5b: the Profile Service mock MCP server (tools/mcp_profile_service_mock.py) had no test
exercising it at all — `get_customer_profile` and the mock's MCP handshake (initialize,
tools/list, tools/call, unknown method/tool) were pinned by nothing, unlike its sibling mocks
(mcp_itsm_mock is read by test_banking_regulatory_compliance.py). This file closes that gap
directly against the mock's FastAPI app, no network and no database required.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from tools import mcp_profile_service_mock as profile_mock

client = TestClient(profile_mock.app)


def test_get_customer_profile_fn_returns_a_shaped_profile():
    result = profile_mock.TOOL_FNS["get_customer_profile"]({"customer_id": "cust-42"})
    assert result == {"customer_id": "cust-42", "segment": "retail", "risk_tier": "standard"}


def test_get_customer_profile_fn_falls_back_to_unknown_for_blank_id():
    assert profile_mock.TOOL_FNS["get_customer_profile"]({})["customer_id"] == "unknown"
    assert profile_mock.TOOL_FNS["get_customer_profile"]({"customer_id": "  "})["customer_id"] == "unknown"


def test_initialize_reports_server_info_and_tool_capability():
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["serverInfo"]["name"] == "boss-profile-service-mock"
    assert body["result"]["capabilities"] == {"tools": {}}


def test_notifications_initialized_is_accepted_with_no_body():
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert resp.status_code == 202


def test_tools_list_advertises_get_customer_profile():
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [t["name"] for t in resp.json()["result"]["tools"]]
    assert names == ["get_customer_profile"]


def test_tools_call_get_customer_profile_round_trips_through_jsonrpc():
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                     "params": {"name": "get_customer_profile",
                                                "arguments": {"customer_id": "cust-7"}}})
    body = resp.json()
    assert body["result"]["isError"] is False
    import json as _json
    payload = _json.loads(body["result"]["content"][0]["text"])
    assert payload == {"customer_id": "cust-7", "segment": "retail", "risk_tier": "standard"}


def test_tools_call_unknown_tool_is_a_jsonrpc_error_not_a_crash():
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                     "params": {"name": "nope", "arguments": {}}})
    body = resp.json()
    assert body["error"]["code"] == -32602
    assert "nope" in body["error"]["message"]


def test_unknown_method_is_a_jsonrpc_error():
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 5, "method": "whatever"})
    body = resp.json()
    assert body["error"]["code"] == -32601


def test_health_lists_the_one_tool():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "server": "boss-profile-service-mock",
                           "tools": ["get_customer_profile"]}
