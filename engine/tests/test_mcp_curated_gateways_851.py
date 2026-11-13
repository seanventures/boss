"""CURATED GATEWAYS FOR OUTSIDE MCP CLIENTS (BOSSAPP-851).

The upstream is a REAL MCP server — the stdio echo fixture, registered through the registry and
bridged by the 850 supervisor — so a gateway call goes the whole way: outside client -> gateway
-> call_tool_with_receipt -> mcp_client -> bridge -> the server, and back with a receipt and an
audit row. Auth, profile filtering, the cap, the registry's own Ask backstop, the meta-tools and
the HTTP surface (401s, 202 for notifications) are each checked.
"""
from __future__ import annotations

import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import mcp_curated_gateways as gw
from boss_engine.services import mcp_registry
from boss_engine.services import mcp_stdio_templates as templates

from ._p29b_support import drop, echo_template, fresh_engine, make_project, subprocess_runtime  # noqa: F401

PROJECT = "proj_gw_a"
OTHER = "proj_gw_b"


@pytest.fixture(scope="module")
def db():
    engine, admin = fresh_engine()
    make_project(engine, PROJECT)
    make_project(engine, OTHER)
    yield engine
    drop(engine, admin)


@pytest.fixture
def settings(db):
    return get_settings().model_copy(update={"database_url": db.url.render_as_string(
        hide_password=False)})


@pytest.fixture
def server(db, settings, subprocess_runtime):
    """The echo server, registered, reviewed, probed (its tools stored), write_note at tier 2."""
    with db.begin() as c:
        found = mcp_registry.find_mcp_server(c, PROJECT, "echo")
        if found is None:
            templates.create_template(c, PROJECT, echo_template("echo"), actor="op")
            found = mcp_registry.register_mcp_server(
                c, PROJECT, "echo", f"stdio://{PROJECT}/echo", transport="stdio", risk_tier=0,
                tool_risk_overrides={"write_note": 2}, actor="op", settings=settings,
                auth={"type": "bearer", "secret": "tok-GW"})
            mcp_registry.review_mcp_server(c, PROJECT, found["id"], actor="op")
    probe = mcp_registry.probe(settings, f"stdio://{PROJECT}/echo",
                               headers={"Authorization": "Bearer tok-GW"})
    assert probe["reachable"], probe
    with db.begin() as c:
        mcp_registry.record_reachability(c, PROJECT, found["id"], reachable=True,
                                         tools=probe["tools"])
        return mcp_registry.get_mcp_server(c, PROJECT, found["id"])


def _make(db, server, name, **over) -> dict:
    profile = over.pop("profile", {"servers": [{"server_id": server["id"], "tools": None,
                                                "caps": {"die": "off"}}]})
    with db.begin() as c:
        return gw.create_gateway(c, PROJECT, name=name, profile=profile, actor="op", **over)


def _rpc(db, settings, gateway, method, params=None, rid=1):
    return gw.handle(db, settings, gateway, {"jsonrpc": "2.0", "id": rid, "method": method,
                                            "params": params or {}})


def _open(db, gateway_id, token):
    with db.begin() as c:
        return gw.authenticate(c, gateway_id, token)


# ------------------------------------------------------------------------ tokens and auth

def test_a_token_is_shown_once_and_only_its_hash_is_kept(db, server):
    made = _make(db, server, "tokens")
    token = made["token"]
    assert token.startswith(gw.TOKEN_PREFIX)
    with db.begin() as c:
        stored = c.execute(text("SELECT token_hash, last4 FROM mcp_gateway_tokens "
                                "WHERE gateway_id = :g"), {"g": made["gateway"]["id"]}).one()
        listed = json.dumps(gw.list_gateways(c, PROJECT), default=str)
    assert stored[0] == hashlib.sha256(token.encode()).hexdigest() and stored[1] == token[-4:]
    assert token not in listed and stored[0] not in listed
    assert made["record"]["status"] == "active"


def test_bad_foreign_expired_and_revoked_tokens_are_refused(db, server):
    one = _make(db, server, "auth-one")
    two = _make(db, server, "auth-two")
    gid = one["gateway"]["id"]
    assert _open(db, gid, one["token"])["id"] == gid
    for bad in ("", "boss_gw_nope", one["token"] + "x", two["token"], "Bearer " + one["token"]):
        with pytest.raises(gw.GatewayAuthError):
            _open(db, gid, bad)
    with db.begin() as c:
        c.execute(text("UPDATE mcp_gateway_tokens SET expires_at = now() - interval '1 minute' "
                       "WHERE id = :t"), {"t": one["record"]["id"]})
    with pytest.raises(gw.GatewayAuthError):
        _open(db, gid, one["token"])
    with db.begin() as c:
        fresh = gw.mint_token(c, PROJECT, gid, actor="op", expires_in_days=1)
    assert _open(db, gid, fresh["token"])
    with db.begin() as c:
        gw.revoke_token(c, PROJECT, gid, fresh["record"]["id"], actor="op")
    with pytest.raises(gw.GatewayAuthError):
        _open(db, gid, fresh["token"])
    tid = two["gateway"]["id"]
    with db.begin() as c:
        gw.revoke_gateway(c, PROJECT, tid, actor="op")
    with pytest.raises(gw.GatewayAuthError):
        _open(db, tid, two["token"])


def test_a_profile_names_only_servers_of_its_own_project(db, server):
    with db.begin() as c:
        with pytest.raises(gw.GatewayError, match="no MCP server"):
            gw.create_gateway(c, OTHER, name="x", actor="op",
                              profile={"servers": [{"server_id": server["id"]}]})
        with pytest.raises(gw.GatewayError, match="'allow' or 'off'"):
            gw.create_gateway(c, PROJECT, name="x", actor="op", profile={"servers": [
                {"server_id": server["id"], "caps": {"echo": "ask"}}]})


# ------------------------------------------------------------------------ the MCP surface

def test_tools_list_serves_the_profile_without_what_needs_a_person(db, settings, server):
    gateway = _open(db, *(lambda m: (m["gateway"]["id"], m["token"]))(_make(db, server, "list")))
    names = {t["name"] for t in _rpc(db, settings, gateway, "tools/list")["result"]["tools"]}
    assert {"echo__echo", "echo__whoami", "echo__stats", "search_tools", "run_tool"} <= names
    assert "echo__write_note" not in names  # tier 2: asks a person; nobody is here
    assert "echo__die" not in names         # capped off by the profile
    narrow = _make(db, server, "narrow", allowed_actions=["tools/list", "tools/call"],
                   profile={"servers": [{"server_id": server["id"], "tools": ["echo"]}]})
    gateway = _open(db, narrow["gateway"]["id"], narrow["token"])
    names = {t["name"] for t in _rpc(db, settings, gateway, "tools/list")["result"]["tools"]}
    assert names == {"echo__echo"}
    refused = _rpc(db, settings, gateway, "tools/call", {"name": "search_tools",
                                                         "arguments": {"query": "x"}})
    assert refused["error"]["code"] == -32601


def test_a_proxied_call_lands_a_receipt_and_an_audit_row(db, settings, server):
    made = _make(db, server, "call")
    gateway = _open(db, made["gateway"]["id"], made["token"])
    answer = _rpc(db, settings, gateway, "tools/call",
                  {"name": "echo__whoami", "arguments": {}})["result"]
    assert answer["isError"] is False
    assert json.loads(answer["content"][0]["text"])["token"] == "tok-GW"
    receipt_id = answer["_meta"]["boss/receipt_id"]
    with db.begin() as c:
        assert c.execute(text("SELECT status FROM receipts WHERE id = :id"),
                         {"id": receipt_id}).scalar_one() == "succeeded"
        rows = c.execute(text(
            "SELECT tool, outcome, decision FROM tool_call_audit WHERE run_id = :r"),
            {"r": f"mcpgw:{gateway['id']}"}).all()
    assert [tuple(r) for r in rows] == [("whoami", "succeeded", "allow")]


def test_the_cap_and_the_registrys_ask_both_refuse_and_both_are_audited(db, settings, server):
    made = _make(db, server, "refusals")
    gateway = _open(db, made["gateway"]["id"], made["token"])
    capped = _rpc(db, settings, gateway, "tools/call", {"name": "echo__die", "arguments": {}})
    asked = _rpc(db, settings, gateway, "tools/call",
                 {"name": "echo__write_note", "arguments": {"text": "x"}})
    assert capped["result"]["isError"] and "not offered" in capped["result"]["content"][0]["text"]
    assert asked["result"]["isError"] and "ask a person" in asked["result"]["content"][0]["text"]
    with db.begin() as c:
        rows = {(r[0], r[1], r[2]) for r in c.execute(text(
            "SELECT tool, outcome, decision FROM tool_call_audit WHERE run_id = :r"),
            {"r": f"mcpgw:{gateway['id']}"})}
    assert rows == {("die", "refused", "off"), ("write_note", "refused", "ask")}
    unknown = _rpc(db, settings, gateway, "tools/call", {"name": "other__x", "arguments": {}})
    assert unknown["result"]["isError"]


def test_the_meta_tools_find_and_run(db, settings, server):
    made = _make(db, server, "meta")
    gateway = _open(db, made["gateway"]["id"], made["token"])
    found = _rpc(db, settings, gateway, "tools/call",
                 {"name": "search_tools", "arguments": {"query": "echo text"}})["result"]
    tools = json.loads(found["content"][0]["text"])["tools"]
    assert tools[0]["name"] == "echo__echo" and "inputSchema" in tools[0]
    ran = _rpc(db, settings, gateway, "tools/call", {"name": "run_tool", "arguments": {
        "name": "echo__echo", "arguments": {"text": "via meta"}}})["result"]
    assert json.loads(ran["content"][0]["text"]) == {"echo": "via meta"}
    assert _rpc(db, settings, gateway, "tools/call", {"name": "run_tool", "arguments": {
        "name": "echo__die"}})["result"]["isError"]


# ------------------------------------------------------------------------ over HTTP

@pytest.fixture
def http(db, settings):
    from apps.api.app.routers.mcp_connect import build_mcp_connect_router

    app = FastAPI()
    app.include_router(build_mcp_connect_router(
        engine=db, settings=lambda: settings,
        require_write_actor=lambda c, p, a, perm: a or "op",
        require_reader=lambda c, p: None))
    return TestClient(app)


def test_the_gateway_over_http_speaks_mcp_and_refuses_without_a_token(db, server, http):
    created = http.post("/v1/mcp/gateways", json={
        "project_id": PROJECT, "actor": "op", "name": "over-http",
        "profile": {"servers": [{"server_id": server["id"]}]}})
    assert created.status_code == 201, created.text
    gid, token = created.json()["gateway"]["id"], created.json()["token"]
    path = f"/mcp/gw/{gid}"
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
           "params": {"protocolVersion": "2025-06-18", "capabilities": {}}}
    nope = http.post(path, json=rpc)
    assert nope.status_code == 401 and "Bearer" in nope.headers["www-authenticate"]
    assert http.post(path, json=rpc, headers={"Authorization": "Bearer boss_gw_x"}).status_code == 401
    auth = {"Authorization": f"Bearer {token}"}
    init = http.post(path, json=rpc, headers=auth)
    assert init.status_code == 200 and init.json()["result"]["protocolVersion"] == "2025-06-18"
    note = http.post(path, json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                     headers=auth)
    assert note.status_code == 202
    listed = http.post(path, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                       headers=auth)
    assert "echo__echo" in {t["name"] for t in listed.json()["result"]["tools"]}
    # The list never carries a token; a second token is shown once; revoking ends access.
    page = http.get(f"/v1/mcp/gateways?project_id={PROJECT}").json()
    assert token not in json.dumps(page)
    minted = http.post(f"/v1/mcp/gateways/{gid}/tokens",
                       json={"project_id": PROJECT, "actor": "op", "expires_in_days": 7})
    assert minted.status_code == 201 and minted.json()["token"].startswith("boss_gw_")
    assert http.post(f"/v1/mcp/gateways/{gid}/revoke",
                     json={"project_id": PROJECT, "actor": "op"}).status_code == 200
    assert http.post(path, json=rpc, headers=auth).status_code == 401


def test_the_connector_and_template_routes(db, http):
    page = http.get(f"/v1/mcp/connectors?project_id={PROJECT}&q=postgres&category=data").json()
    pg = next(i for i in page["items"] if i["key"] == "postgres")
    assert pg["template_ready"] is False and all(i["category"] == "data" for i in page["items"])
    tpl = pg["prefill"]["stdio_template"]
    made = http.post("/v1/mcp/stdio-templates", json={"project_id": PROJECT, "actor": "op",
                                                       **tpl})
    assert made.status_code == 201, made.text
    again = http.get(f"/v1/mcp/connectors?project_id={PROJECT}&q=postgres").json()
    assert next(i for i in again["items"] if i["key"] == "postgres")["template_ready"] is True
    dup = http.post("/v1/mcp/stdio-templates", json={"project_id": PROJECT, "actor": "op",
                                                      **tpl})
    assert dup.status_code == 409
    bad = http.post("/v1/mcp/stdio-templates", json={"project_id": PROJECT, "actor": "op",
                                                      **{**tpl, "name": "pg2", "network": "host"}})
    assert bad.status_code == 422
    tid = made.json()["id"]
    edited = http.patch(f"/v1/mcp/stdio-templates/{tid}?project_id={PROJECT}",
                        json={"project_id": PROJECT, "actor": "op",
                              "changes": {"idle_ttl_seconds": 120}})
    assert edited.status_code == 200 and edited.json()["idle_ttl_seconds"] == 120
    assert http.delete(f"/v1/mcp/stdio-templates/{tid}?project_id={PROJECT}&actor=op"
                       ).status_code == 204
