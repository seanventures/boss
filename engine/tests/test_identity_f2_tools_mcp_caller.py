"""M8 (= sec-agents #9): the MCP API-surface tools call the API AS THE CALLER, never with the
server's configured key, and the shared BOSS_MCP_HTTP_TOKEN without an identity is refused under
enforce. No database: the API is an httpx mock that records every Authorization it receives.
"""
from __future__ import annotations

import json

import httpx
import pytest

from boss_engine.config import get_settings
from boss_engine.identity.enforcement import LEDGER
from boss_engine.identity.principal import AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus
from boss_engine.mcp import server as mcp_server
from boss_engine.mcp.api_surface import CALLER_AUTHORIZATION, ApiSurface
from boss_engine.tools.registry import ToolRegistry

ALICE = AuthContext(Principal("usr_a", PrincipalKind.USER, "org_x", PrincipalStatus.ACTIVE, "A"),
                    AuthMethod.API_KEY)
SERVER_KEY = "server-configured-key"

SCHEMA = {"openapi": "3.1.0", "paths": {
    "/v1/things": {"get": {"summary": "List things", "parameters": [
        {"name": "project_id", "in": "query", "required": True, "schema": {"type": "string"}}]}},
}}


class _Ctx:
    durable_workspace_configured = True


def _surface():
    seen: list[httpx.Request] = []

    def send(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(send),
                          headers={"Authorization": f"Bearer {SERVER_KEY}"})
    surface = ApiSurface(SCHEMA, client=client, principal="mcp-member", pinned_project_id="p1")
    surface.launch_authorization = f"Bearer {SERVER_KEY}"
    return surface, seen


@pytest.fixture
def mode(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "authz_router_modes", "")
    LEDGER.clear()

    def set_mode(value: str) -> None:
        monkeypatch.setattr(settings, "authz_mode", value)

    yield set_mode
    LEDGER.clear()


def test_without_a_caller_credential_nothing_is_sent_under_the_server_key():
    surface, seen = _surface()
    out = surface.invoke("get_v1_things" if "get_v1_things" in surface.operations
                         else next(iter(surface.operations)), project_id="p1")
    assert out["ok"] is False and out["error"] == "caller_identity_required"
    assert seen == []


def test_the_api_receives_the_callers_own_credential_not_the_servers():
    surface, seen = _surface()
    alias = next(iter(surface.operations))
    token = CALLER_AUTHORIZATION.set("Bearer boss_pat_caller")
    try:
        assert surface.invoke(alias, project_id="p1")["ok"] is True
    finally:
        CALLER_AUTHORIZATION.reset(token)
    assert [r.headers["authorization"] for r in seen] == ["Bearer boss_pat_caller"]
    assert SERVER_KEY not in json.dumps([dict(r.headers) for r in seen])


def _server(surface, **kw):
    reg = ToolRegistry()
    return mcp_server.MCPServer(_Ctx(), project_id="p1", registry=reg, api_surface=surface,
                                api_domains={"all"}, **kw)


def _call(client, alias, auth):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "api_read_operation", "arguments": {"operation": alias}}}
    return client.post("/mcp", json=body, headers={"Authorization": auth})


def test_http_mcp_forwards_each_requests_own_bearer(mode, monkeypatch):
    from fastapi.testclient import TestClient

    mode("enforce")
    surface, seen = _surface()
    good = "Bearer boss_pat_" + "a" * 40
    monkeypatch.setattr(mcp_server, "_is_boss_credential", lambda t: t.startswith("boss_pat_"))
    monkeypatch.setattr(mcp_server, "authenticate_transport",
                        lambda headers, **k: ALICE if headers.get("authorization") == good else None)
    client = TestClient(_server(surface, http_token="shared-token").asgi_app())
    alias = next(iter(surface.operations))
    resp = _call(client, alias, good)
    payload = json.loads(resp.json()["result"]["content"][0]["text"])
    assert payload["ok"] is True, payload
    assert [r.headers["authorization"] for r in seen] == [good]
    # the shared token identifies nobody: refused at the transport under enforce
    assert _call(client, alias, "Bearer shared-token").status_code == 401
    assert len(seen) == 1


def test_shadow_shared_token_never_borrows_the_server_key(mode):
    from fastapi.testclient import TestClient

    mode("shadow")
    surface, seen = _surface()
    client = TestClient(_server(surface, http_token="shared-token").asgi_app())
    resp = _call(client, next(iter(surface.operations)), "Bearer shared-token")
    payload = json.loads(resp.json()["result"]["content"][0]["text"])
    assert payload["ok"] is False and payload["error"] == "caller_identity_required"
    assert seen == []


def test_stdio_calls_as_the_key_the_session_was_launched_with(mode):
    mode("enforce")
    surface, seen = _surface()
    server = _server(surface, identity=lambda: ALICE,
                     session_credential="Bearer boss_pat_session")
    out = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "api_read_operation",
                                    "arguments": {"operation": next(iter(surface.operations))}}})
    assert json.loads(out["result"]["content"][0]["text"])["ok"] is True
    assert [r.headers["authorization"] for r in seen] == ["Bearer boss_pat_session"]
