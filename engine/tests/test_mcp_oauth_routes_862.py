"""BOSSAPP-862 over HTTP: the public OAuth endpoints a remote MCP client calls (metadata, DCR,
authorize redirect, token, device), and the MCP server's RFC 9728 pointer."""
from __future__ import annotations

import os
import secrets
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL or "test" not in URL.lower(),
                                reason="needs BOSS_TEST_DATABASE_URL naming a disposable database")
REDIRECT = "http://127.0.0.1:33418/callback"


@pytest.fixture(scope="module")
def eng():
    from boss_engine.db import ensure_schema

    engine = create_engine(URL, future=True)
    ensure_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def client(eng, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apps.api.app.routes_mcp_oauth import build_mcp_oauth_router

    monkeypatch.setenv("BOSS_MCP_OAUTH_ISSUER", "https://boss.example.com")
    app = FastAPI()
    app.include_router(build_mcp_oauth_router(lambda: eng))
    return TestClient(app)


@pytest.fixture()
def person(eng):
    from boss_engine.identity import authz

    sfx = uuid4().hex[:8]
    w = {"org": f"org_862h_{sfx}", "user": f"usr_862h_{sfx}"}
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'o')"), {"o": w["org"]})
        c.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) "
                       "VALUES (:u, :o, 'user', 'active', 'Ada')"), {"u": w["user"], "o": w["org"]})
        c.execute(text("INSERT INTO identity_role_bindings (id, org_id, subject_type, subject_id, role_id, "
                       "scope_type, scope_id, granted_by) VALUES (:id, :o, 'principal', :u, 'arl_viewer', "
                       "'org', :o, 'test')"), {"id": f"bnd_{sfx}", "o": w["org"], "u": w["user"]})
    authz.HELD_CACHE.clear()
    return w


def test_a_remote_client_registers_authorizes_and_gets_a_token(eng, client, person):
    from boss_engine.services import mcp_oauth_server as m

    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["registration_endpoint"] == "https://boss.example.com/v1/mcp-oauth/register"
    reg = client.post("/v1/mcp-oauth/register", json={"client_name": "Cursor", "redirect_uris": [REDIRECT]})
    assert reg.status_code == 201
    cid = reg.json()["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = m.s256(verifier)
    go = client.get("/v1/mcp-oauth/authorize", params={
        "client_id": cid, "redirect_uri": REDIRECT, "code_challenge": challenge,
        "code_challenge_method": "S256", "response_type": "code", "state": "s1"}, follow_redirects=False)
    assert go.status_code == 302
    assert go.headers["location"].startswith("https://boss.example.com/connected-apps/authorize?")
    bad = client.get("/v1/mcp-oauth/authorize", params={"client_id": cid, "redirect_uri": "https://evil/cb",
                                                         "code_challenge": challenge,
                                                         "code_challenge_method": "S256"},
                     follow_redirects=False)
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_request"
    # the person consents in the console (session-authenticated; the service is what it calls)
    with eng.begin() as c:
        back = m.authorize(c, principal_id=person["user"], org_id=person["org"], client_id=cid,
                           redirect_uri=REDIRECT, code_challenge=challenge, state="s1")
    code = parse_qs(urlsplit(back).query)["code"][0]
    tok = client.post("/v1/mcp-oauth/token", data={
        "grant_type": "authorization_code", "client_id": cid, "code": code,
        "redirect_uri": REDIRECT, "code_verifier": verifier})
    assert tok.status_code == 200 and tok.headers["cache-control"] == "no-store"
    assert tok.json()["access_token"].startswith("boss_pat_")
    again = client.post("/v1/mcp-oauth/token", data={
        "grant_type": "authorization_code", "client_id": cid, "code": code,
        "redirect_uri": REDIRECT, "code_verifier": verifier})
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
    assert client.post("/v1/mcp-oauth/token", data={"grant_type": "password"}).json()["error"] \
        == "unsupported_grant_type"


def test_a_failed_pkce_attempt_still_spends_the_code(eng, client, person):
    from boss_engine.services import mcp_oauth_server as m

    cid = client.post("/v1/mcp-oauth/register", json={"redirect_uris": [REDIRECT]}).json()["client_id"]
    verifier = secrets.token_urlsafe(48)
    with eng.begin() as c:
        back = m.authorize(c, principal_id=person["user"], org_id=person["org"], client_id=cid,
                           redirect_uri=REDIRECT, code_challenge=m.s256(verifier))
    code = parse_qs(urlsplit(back).query)["code"][0]
    wrong = client.post("/v1/mcp-oauth/token", data={"grant_type": "authorization_code", "client_id": cid,
                                                      "code": code, "redirect_uri": REDIRECT,
                                                      "code_verifier": "y" * 60})
    assert wrong.status_code == 400
    right = client.post("/v1/mcp-oauth/token", data={"grant_type": "authorization_code", "client_id": cid,
                                                      "code": code, "redirect_uri": REDIRECT,
                                                      "code_verifier": verifier})
    assert right.status_code == 400 and right.json()["error"] == "invalid_grant"


def test_device_endpoint_and_consent_needs_a_person(client):
    reg = client.post("/v1/mcp-oauth/register", json={
        "client_name": "boss login", "redirect_uris": [], "grant_types": ["urn:ietf:params:oauth:grant-type:device_code"]})
    started = client.post("/v1/mcp-oauth/device_authorization", data={"client_id": reg.json()["client_id"]}).json()
    assert started["verification_uri"] == "https://boss.example.com/connected-apps/device"
    poll = client.post("/v1/mcp-oauth/token", data={
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": reg.json()["client_id"], "device_code": started["device_code"]})
    assert poll.json()["error"] == "authorization_pending"
    assert client.post("/v1/mcp-oauth/consent", json={}).status_code == 401
    assert client.post("/v1/mcp-oauth/device/approve", json={}).status_code == 401


def test_the_mcp_server_points_at_its_authorization_server(monkeypatch):
    from boss_engine.mcp import server as srv

    monkeypatch.delenv("BOSS_MCP_OAUTH_ISSUER", raising=False)
    assert srv._bearer_challenge() == "Bearer" and srv.oauth_resource_metadata() is None
    monkeypatch.setenv("BOSS_MCP_OAUTH_ISSUER", "https://boss.example.com")
    assert srv._bearer_challenge() == \
        'Bearer resource_metadata="https://boss.example.com/.well-known/oauth-protected-resource"'
    assert srv.oauth_resource_metadata()["resource"] == "https://boss.example.com/mcp"
