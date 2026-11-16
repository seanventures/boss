"""An MCP server's token is used, and never read back (skills-mcp E2, test 2).

`mcp_servers` had nowhere to keep a token, so no hosted MCP server could be used. Now `auth`
holds a secret-store REFERENCE: the server sees the bearer header on every call, and no read —
the list, the Kit, an export — carries any part of the secret but its last four.
"""
from __future__ import annotations

import json
import uuid

import httpx
import pytest

PROJECT = "boss-dogfood"
SECRET = "sk-live-" + "a" * 20 + "9876"


@pytest.fixture()
def fake_server(monkeypatch):
    from boss_engine.services import mcp_client

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        seen.append(dict(request.headers))
        if request.headers.get("authorization") != f"Bearer {SECRET}":
            return httpx.Response(401)
        if body.get("method") == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})
        if body.get("method") == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
                "tools": [{"name": "list_issues", "inputSchema": {"type": "object"}}]}})
        return httpx.Response(202)

    transport = httpx.MockTransport(handler)

    def post(url, **kwargs):
        kwargs.pop("timeout", None)
        with httpx.Client(transport=transport) as client:
            return client.post(url, **kwargs)

    monkeypatch.setattr(mcp_client.httpx, "post", post)
    mcp_client.forget_sessions()
    return seen


def test_the_server_sees_the_token_and_no_read_contains_it(client, fake_server):
    name = f"tracker_{uuid.uuid4().hex[:6]}"
    made = client.post("/v1/mcp/servers", json={
        "project_id": PROJECT, "actor": "ceo", "name": name, "url": "https://mcp.example/mcp",
        "auth": {"type": "bearer", "secret": SECRET}})
    assert made.status_code == 201, made.text
    assert SECRET not in made.text and made.json()["auth"] == {
        "type": "bearer", "header_name": None, "set": True, "last4": "9876", "headers": {}}

    listed = client.get("/v1/mcp/servers", params={"project_id": PROJECT, "check": "now"})
    assert listed.status_code == 200
    assert any(h.get("authorization") == f"Bearer {SECRET}" for h in fake_server), \
        "the probe did not sign in"
    row = next(s for s in listed.json()["items"] if s["name"] == name)
    assert row["reachable"] is True, row["last_error"]

    server_id = made.json()["id"]
    reads = [listed.text,
             client.get("/v1/kit", params={"project_id": PROJECT, "check": "none"}).text,
             client.get(f"/v1/mcp/servers/{server_id}/export",
                        params={"project_id": PROJECT, "actor": "ceo"}).text]
    for read in reads:
        assert SECRET not in read and SECRET[:12] not in read
        assert "secret_ref_id" not in read


def test_save_and_test_a_typed_token_stores_nothing(client, fake_server):
    before = client.get("/v1/mcp/servers", params={"project_id": PROJECT}).json()["items"]
    tested = client.post("/v1/mcp/servers/test", json={
        "project_id": PROJECT, "actor": "ceo", "url": "https://mcp.example/mcp",
        "auth": {"type": "bearer", "secret": SECRET}})
    assert tested.status_code == 200 and tested.json()["ok"] is True
    assert [t["name"] for t in tested.json()["tools"]] == ["list_issues"]
    wrong = client.post("/v1/mcp/servers/test", json={
        "project_id": PROJECT, "actor": "ceo", "url": "https://mcp.example/mcp",
        "auth": {"type": "bearer", "secret": "wrong-token-0000"}})
    assert wrong.json()["ok"] is False and wrong.json()["error"].startswith("401")
    after = client.get("/v1/mcp/servers", params={"project_id": PROJECT}).json()["items"]
    assert len(after) == len(before)


def test_a_server_added_from_the_console_keeps_the_tools_the_test_found(client, fake_server):
    """Save & test, then register, is how the console adds one remote server. The tools that
    test found are what the drawer reads back, with no second probe and no live server."""
    name = f"orders_{uuid.uuid4().hex[:6]}"
    url = "https://mcp.example/mcp"
    tested = client.post("/v1/mcp/servers/test", json={
        "project_id": PROJECT, "actor": "ceo", "url": url,
        "auth": {"type": "bearer", "secret": SECRET}})
    assert tested.status_code == 200
    assert [t["name"] for t in tested.json()["tools"]] == ["list_issues"]
    probed = len(fake_server)
    made = client.post("/v1/mcp/servers", json={
        "project_id": PROJECT, "actor": "ceo", "name": name, "url": url,
        "auth": {"type": "bearer", "secret": SECRET}})
    assert made.status_code == 201, made.text
    assert [t["name"] for t in made.json()["tools"]] == ["list_issues"]
    assert made.json()["reachable"] is True
    listed = client.get("/v1/mcp/servers", params={"project_id": PROJECT, "check": "none"})
    row = next(s for s in listed.json()["items"] if s["name"] == name)
    assert [t["name"] for t in row["tools"]] == ["list_issues"]
    assert row["reachable"] is True
    assert len(fake_server) == probed, "register probed the server again"
    assert SECRET not in made.text and SECRET not in listed.text


def test_registering_without_a_passing_test_does_not_invent_tools(client, fake_server):
    name = f"quiet_{uuid.uuid4().hex[:6]}"
    url = "https://mcp.example/never-tested"
    made = client.post("/v1/mcp/servers", json={
        "project_id": PROJECT, "actor": "ceo", "name": name, "url": url,
        "auth": {"type": "bearer", "secret": SECRET}})
    assert made.status_code == 201, made.text
    assert made.json()["tools"] == []
    assert made.json()["reachable"] is None
    listed = client.get("/v1/mcp/servers", params={"project_id": PROJECT, "check": "none"})
    row = next(s for s in listed.json()["items"] if s["name"] == name)
    assert row["tools"] == []
    assert fake_server == []


def test_a_url_change_voids_the_review_and_a_review_records_who(client, fake_server):
    made = client.post("/v1/mcp/servers", json={
        "project_id": PROJECT, "actor": "ceo", "name": f"t_{uuid.uuid4().hex[:6]}",
        "url": "https://mcp.example/mcp", "auth": {"type": "bearer", "secret": SECRET}}).json()
    assert made["trusted"] is False
    reviewed = client.post(f"/v1/mcp/servers/{made['id']}/review",
                           json={"project_id": PROJECT, "actor": "ceo"})
    assert reviewed.status_code == 200 and reviewed.json()["trusted"] is True
    assert reviewed.json()["provenance"]["reviewed_by"] == "ceo"
    moved = client.patch(f"/v1/mcp/servers/{made['id']}", params={"project_id": PROJECT},
                         json={"url": "https://elsewhere.example/mcp"})
    assert moved.status_code == 200 and moved.json()["trusted"] is False
    # The token survived the edit (not retyped) and is still never shown.
    assert moved.json()["auth"]["set"] is True and SECRET not in moved.text
