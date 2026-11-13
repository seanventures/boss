"""BOSSAPP-846..848 over HTTP: Sign in, the public callback, credentials, policy, grants.

The OAuth protocol itself is covered end to end against a fake provider in
engine/tests/test_mcp_oauth_and_credentials.py; here the provider's three network steps are
stubbed so the routes, their permissions and their answers are what is under test.
"""
from __future__ import annotations

import base64
import json
import os
from urllib.parse import parse_qs, urlsplit

import pytest

os.environ.setdefault("BOSS_MASTER_KEY", base64.b64encode(b"p29a-test-master-key-32-bytes!!!").decode())

PROJECT = "boss-dogfood"


class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    def json(self):
        return self._body


@pytest.fixture()
def provider(monkeypatch):
    from boss_engine.identity import vault
    from boss_engine.services import mcp_oauth

    vault.reset_master_key_cache()
    monkeypatch.setenv("BOSS_PUBLIC_URL", "https://boss.example.test")
    seen: dict = {"token_forms": []}
    monkeypatch.setattr(mcp_oauth, "discover", lambda url: {
        "resource": url, "authorization_server": "https://as.example.test",
        "authorization_endpoint": "https://as.example.test/authorize",
        "token_endpoint": "https://as.example.test/token",
        "registration_endpoint": "https://as.example.test/register",
        "token_endpoint_auth_methods": ["none"], "discovered_at": "now"})
    monkeypatch.setattr(mcp_oauth, "guard_url", lambda url, what: url)
    monkeypatch.setattr(mcp_oauth, "register_client", lambda meta, redirect: {
        "client_id": "cid-api", "client_secret": None, "token_endpoint_auth_method": "none"})

    def token(oauth, secret, form):
        seen["token_forms"].append(form)
        return _Resp(200, {"access_token": "api-access-token", "token_type": "bearer",
                           "expires_in": 3600, "refresh_token": "api-refresh"})

    monkeypatch.setattr(mcp_oauth, "_token_request", token)
    return seen


def _drop_test_servers(engine):
    from sqlalchemy import text

    from boss_engine.services import mcp_registry as reg

    with engine.begin() as c:
        for sid in c.execute(text("SELECT id FROM mcp_servers WHERE project_id = :p AND name "
                                  "LIKE '%-p29a'"), {"p": PROJECT}).scalars().all():
            reg.delete_mcp_server(c, PROJECT, sid, "test", release_grants=True)


@pytest.fixture(autouse=True)
def _no_leftover_servers(engine):
    _drop_test_servers(engine)
    yield
    _drop_test_servers(engine)


@pytest.fixture()
def server_id(engine):
    from boss_engine.services import mcp_registry as reg

    with engine.begin() as c:
        server = reg.register_mcp_server(c, PROJECT, "linear-p29a", "https://mcp.linear.test/mcp",
                                         risk_tier=0, actor="ceo", auth={"type": "oauth"})
    return server["id"]


def test_sign_in_then_the_public_callback_keeps_the_token_out_of_every_answer(client, provider,
                                                                             server_id):
    started = client.post(f"/v1/mcp/servers/{server_id}/oauth/start",
                          json={"project_id": PROJECT, "actor": "ceo", "subject": "org"})
    assert started.status_code == 200, started.text
    query = parse_qs(urlsplit(started.json()["redirect"]).query)
    assert query["redirect_uri"] == ["https://boss.example.test/v1/mcp/oauth/callback"]
    assert query["code_challenge_method"] == ["S256"] and query["client_id"] == ["cid-api"]
    state = query["state"][0]
    done = client.get("/v1/mcp/oauth/callback", params={"state": state, "code": "abc"})
    assert done.status_code == 200 and "Signed in" in done.text
    assert provider["token_forms"][-1]["code_verifier"]
    replay = client.get("/v1/mcp/oauth/callback", params={"state": state, "code": "abc"})
    assert replay.status_code == 400 and "Not signed in" in replay.text
    listed = client.get(f"/v1/mcp/servers/{server_id}/credentials",
                        params={"project_id": PROJECT})
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["auth_status"] == "connected"
    assert [c["subject_kind"] for c in body["credentials"]] == ["org"]
    servers = client.get("/v1/mcp/servers", params={"project_id": PROJECT})
    for text_ in (listed.text, servers.text):
        assert "api-access-token" not in text_ and "api-refresh" not in text_
    # Disconnect: the server says so.
    cred = body["credentials"][0]["id"]
    gone = client.delete(f"/v1/mcp/servers/{server_id}/credentials/{cred}",
                         params={"project_id": PROJECT, "actor": "ceo"})
    assert gone.status_code == 200, gone.text
    after = client.get(f"/v1/mcp/servers/{server_id}/credentials", params={"project_id": PROJECT})
    assert after.json()["auth_status"] == "not_connected" and after.json()["credentials"] == []


def test_a_forged_callback_does_nothing(client, provider, server_id):
    r = client.get("/v1/mcp/oauth/callback", params={"state": "made-up", "code": "x"})
    assert r.status_code == 400 and "not one BOSS started" in r.text
    assert provider["token_forms"] == []


def test_use_my_own_account_needs_a_signed_in_person(client, provider, server_id):
    r = client.post(f"/v1/mcp/servers/{server_id}/oauth/mine",
                    json={"project_id": PROJECT, "actor": "ceo"})
    assert r.status_code == 403 and "as yourself" in r.text


def test_an_agents_own_sign_in_and_the_credential_policy(client, provider, server_id):
    r = client.post(f"/v1/mcp/servers/{server_id}/credential-policy",
                    json={"project_id": PROJECT, "actor": "ceo", "policy": "per_agent"})
    assert r.status_code == 200 and r.json()["credential_policy"] == "per_agent"
    bad = client.post(f"/v1/mcp/servers/{server_id}/credential-policy",
                      json={"project_id": PROJECT, "actor": "ceo", "policy": "everyone"})
    assert bad.status_code == 422
    nobody = client.post(f"/v1/mcp/servers/{server_id}/oauth/start",
                         json={"project_id": PROJECT, "actor": "ceo", "subject": "agent",
                               "role_key": "no_such_agent"})
    assert nobody.status_code == 422
    started = client.post(f"/v1/mcp/servers/{server_id}/oauth/start",
                          json={"project_id": PROJECT, "actor": "ceo", "subject": "agent",
                                "role_key": "pm"})
    assert started.status_code == 200, started.text
    state = parse_qs(urlsplit(started.json()["redirect"]).query)["state"][0]
    assert client.get("/v1/mcp/oauth/callback", params={"state": state, "code": "c"}).status_code \
        == 200
    rows = client.get(f"/v1/mcp/servers/{server_id}/credentials",
                      params={"project_id": PROJECT}).json()["credentials"]
    assert [(c["subject_kind"], c["subject_id"]) for c in rows] == [("agent", "pm")]


def test_a_granted_tool_is_listed_and_revocable_where_holders_are_managed(client, engine):
    from boss_engine.config import get_settings
    from boss_engine.services import mcp_registry as reg
    from boss_engine.services import tool_access_requests as tar
    from boss_engine.services.decisions import DecisionService

    with engine.begin() as c:
        server = reg.register_mcp_server(c, PROJECT, "tracker-p29a", "http://tracker:9/mcp",
                                         risk_tier=0, actor="ceo")
        reg.review_mcp_server(c, PROJECT, server["id"], actor="ceo")
        reg.record_reachability(c, PROJECT, server["id"], reachable=True, tools=[
            {"name": "read_issue", "inputSchema": {"type": "object"}}])
        asked = tar.request_access(c, PROJECT, role_key="pm", server_ref="tracker-p29a",
                                   tools=["read_issue"], reason="To read the bug report.",
                                   actor="agent:pm")
    with engine.begin() as c:
        DecisionService(get_settings()).transition(c, asked["decision_id"], action="approve",
                                                   actor="ceo", note=None)
    listed = client.get("/v1/mcp/access-grants",
                        params={"project_id": PROJECT, "server_id": server["id"]})
    assert listed.status_code == 200, listed.text
    [grant] = listed.json()["grants"]
    assert (grant["role_key"], grant["tool"], grant["policy"]) == ("pm", "read_issue", "allow")
    revoked = client.post(f"/v1/mcp/access-grants/{grant['id']}/revoke",
                          json={"project_id": PROJECT, "actor": "ceo"})
    assert revoked.status_code == 200 and revoked.json()["status"] == "revoked"
    again = client.post(f"/v1/mcp/access-grants/{grant['id']}/revoke",
                        json={"project_id": PROJECT, "actor": "ceo"})
    assert again.status_code == 422
    assert json.loads(client.get("/v1/mcp/access-grants", params={
        "project_id": PROJECT, "server_id": server["id"]}).text)["grants"] == []
