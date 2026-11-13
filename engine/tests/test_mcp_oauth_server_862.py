"""BOSSAPP-862: OAuth 2.1 (DCR + code/PKCE + refresh rotation) and the device grant for BOSS's own
MCP server. A granted access token is a scoped, short-lived BOSS personal API key that the MCP
transport verifies; the refresh token is hashed and rotated, and a reused one revokes the grant."""
from __future__ import annotations

import os
import secrets
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import mcp_oauth_server as m

URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL or "test" not in URL.lower(),
                                reason="needs BOSS_TEST_DATABASE_URL naming a disposable database")
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture(scope="module")
def eng():
    from boss_engine.db import ensure_schema

    engine = create_engine(URL, future=True)
    ensure_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def person(eng):
    from boss_engine.identity import authz

    sfx = uuid4().hex[:8]
    w = {"org": f"org_862_{sfx}", "user": f"usr_862_{sfx}"}
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'o')"), {"o": w["org"]})
        c.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) "
                       "VALUES (:u, :o, 'user', 'active', 'Ada')"), w | {"u": w["user"], "o": w["org"]})
        c.execute(text("INSERT INTO identity_role_bindings (id, org_id, subject_type, subject_id, role_id, "
                       "scope_type, scope_id, granted_by) VALUES (:id, :o, 'principal', :u, 'arl_viewer', "
                       "'org', :o, 'test')"), {"id": f"bnd_{sfx}", "o": w["org"], "u": w["user"]})
    authz.HELD_CACHE.clear()
    return w


def _client(eng, grants=None):
    with eng.begin() as c:
        return m.register_client(c, {"client_name": "Claude", "redirect_uris": [REDIRECT],
                                     **({"grant_types": grants} if grants else {})})


def _pkce():
    verifier = secrets.token_urlsafe(48)
    return verifier, m.s256(verifier)


def _key_row(eng, token):
    from boss_engine.identity.authn import hash_token

    with eng.begin() as c:
        return c.execute(text("SELECT id, scopes, revoked_at, expires_at, principal_id FROM "
                              "identity_api_credentials WHERE token_hash = :h"),
                         {"h": hash_token(token)}).mappings().first()


def test_metadata_points_clients_at_the_endpoints():
    meta = m.authorization_server_metadata("https://boss.example.com/")
    assert meta["issuer"] == "https://boss.example.com"
    assert meta["token_endpoint"] == "https://boss.example.com/v1/mcp-oauth/token"
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert m.DEVICE_GRANT in meta["grant_types_supported"]
    prm = m.protected_resource_metadata("https://mcp.example.com/mcp", "https://boss.example.com")
    assert prm["authorization_servers"] == ["https://boss.example.com"]


def test_registration_refuses_unsafe_redirects(eng):
    with eng.begin() as c:
        for bad in (["http://evil.example.com/cb"], ["javascript:alert(1)"], ["https://x/cb#frag"]):
            with pytest.raises(m.OAuthError):
                m.register_client(c, {"redirect_uris": bad})
        ok = m.register_client(c, {"redirect_uris": ["http://127.0.0.1:8765/cb"]})
    assert ok["client_id"].startswith("mcpc_") and ok["token_endpoint_auth_method"] == "none"


def test_code_with_pkce_yields_a_scoped_boss_key_and_rotating_refresh(eng, person):
    client = _client(eng)
    verifier, challenge = _pkce()
    with eng.begin() as c:
        url = m.authorize(c, principal_id=person["user"], org_id=person["org"],
                          client_id=client["client_id"], redirect_uri=REDIRECT,
                          code_challenge=challenge, scope="mcp:read", state="xyz")
    q = parse_qs(urlsplit(url).query)
    assert q["state"] == ["xyz"]
    code = q["code"][0]
    with eng.begin() as c, pytest.raises(m.OAuthError, match="PKCE"):
        m.exchange_code(c, client_id=client["client_id"], code=code, redirect_uri=REDIRECT,
                        code_verifier="x" * 50)
    # the code was spent by that failed attempt: it never works twice
    with eng.begin() as c, pytest.raises(m.OAuthError):
        m.exchange_code(c, client_id=client["client_id"], code=code, redirect_uri=REDIRECT,
                        code_verifier=verifier)
    with eng.begin() as c:
        url = m.authorize(c, principal_id=person["user"], org_id=person["org"],
                          client_id=client["client_id"], redirect_uri=REDIRECT,
                          code_challenge=challenge, scope="mcp:read")
        tokens = m.exchange_code(c, client_id=client["client_id"],
                                 code=parse_qs(urlsplit(url).query)["code"][0],
                                 redirect_uri=REDIRECT, code_verifier=verifier)
    assert tokens["access_token"].startswith("boss_pat_") and tokens["scope"] == "mcp:read"
    key = _key_row(eng, tokens["access_token"])
    assert key["principal_id"] == person["user"] and key["expires_at"] is not None
    assert key["scopes"] and all(s.endswith(":read") for s in key["scopes"])
    # rotate
    with eng.begin() as c:
        rotated = m.refresh(c, client_id=client["client_id"], refresh_token=tokens["refresh_token"])
    assert rotated["refresh_token"] != tokens["refresh_token"]
    assert _key_row(eng, tokens["access_token"])["revoked_at"] is not None     # old key dies
    assert _key_row(eng, rotated["access_token"])["revoked_at"] is None
    # reuse of the spent refresh token revokes the whole grant
    with eng.begin() as c, pytest.raises(m.OAuthError):
        m.refresh(c, client_id=client["client_id"], refresh_token=tokens["refresh_token"])
    assert _key_row(eng, rotated["access_token"])["revoked_at"] is not None
    with eng.begin() as c:
        assert m.connected_apps(c, person["user"]) == []


def test_the_minted_key_authenticates_at_the_mcp_transport(eng, person):
    client = _client(eng)
    verifier, challenge = _pkce()
    with eng.begin() as c:
        url = m.authorize(c, principal_id=person["user"], org_id=person["org"],
                          client_id=client["client_id"], redirect_uri=REDIRECT, code_challenge=challenge)
        tokens = m.exchange_code(c, client_id=client["client_id"],
                                 code=parse_qs(urlsplit(url).query)["code"][0],
                                 redirect_uri=REDIRECT, code_verifier=verifier)
    from boss_engine.mcp.server import authenticate_transport

    ctx = authenticate_transport({"authorization": f"Bearer {tokens['access_token']}"})
    assert ctx is not None and ctx.principal.id == person["user"]
    with eng.begin() as c:
        apps = m.connected_apps(c, person["user"])
        assert [a["client_name"] for a in apps] == ["Claude"]
        assert m.revoke_grant(c, apps[0]["id"], principal_id=person["user"])
    assert _key_row(eng, tokens["access_token"])["revoked_at"] is not None


def test_a_redirect_uri_must_be_registered_and_pkce_is_required(eng, person):
    client = _client(eng)
    with eng.begin() as c:
        with pytest.raises(m.OAuthError, match="not registered"):
            m.check_authorize_request(c, client_id=client["client_id"], redirect_uri="https://evil/cb",
                                      code_challenge="a" * 43, code_challenge_method="S256")
        with pytest.raises(m.OAuthError, match="PKCE"):
            m.check_authorize_request(c, client_id=client["client_id"], redirect_uri=REDIRECT,
                                      code_challenge="a" * 43, code_challenge_method="plain")
        denied = m.authorize(c, principal_id=person["user"], org_id=person["org"],
                             client_id=client["client_id"], redirect_uri=REDIRECT,
                             code_challenge="a" * 43, approve=False, state="s")
    assert "error=access_denied" in denied and "state=s" in denied


def test_device_login(eng, person):
    client = _client(eng, grants=[m.DEVICE_GRANT, "refresh_token"])
    with eng.begin() as c:
        started = m.device_authorize(c, client_id=client["client_id"], scope="mcp",
                                     verification_uri="https://boss.example.com/device")
    assert len(started["user_code"]) == 9 and started["interval"] == 5
    with eng.begin() as c, pytest.raises(m.OAuthError) as pending:
        m.device_token(c, client_id=client["client_id"], device_code=started["device_code"])
    assert pending.value.error == "authorization_pending"
    with eng.begin() as c, pytest.raises(m.OAuthError) as slow:
        m.device_token(c, client_id=client["client_id"], device_code=started["device_code"])
    assert slow.value.error == "slow_down"
    with eng.begin() as c:
        m.device_decide(c, user_code=started["user_code"].replace("-", "").lower(),
                        principal_id=person["user"], org_id=person["org"], approve=True)
        tokens = m.device_token(c, client_id=client["client_id"], device_code=started["device_code"])
    assert tokens["access_token"].startswith("boss_pat_")
    with eng.begin() as c, pytest.raises(m.OAuthError):
        m.device_token(c, client_id=client["client_id"], device_code=started["device_code"])


def test_a_denied_device_code_says_so_and_a_code_client_cannot_use_the_device_grant(eng, person):
    code_client = _client(eng)
    with eng.begin() as c, pytest.raises(m.OAuthError) as refused:
        m.device_authorize(c, client_id=code_client["client_id"], scope="mcp", verification_uri="x")
    assert refused.value.error == "unauthorized_client"
    client = _client(eng, grants=[m.DEVICE_GRANT])
    with eng.begin() as c:
        started = m.device_authorize(c, client_id=client["client_id"], scope=None, verification_uri="x")
        m.device_decide(c, user_code=started["user_code"], principal_id=person["user"],
                        org_id=person["org"], approve=False)
    with eng.begin() as c, pytest.raises(m.OAuthError) as denied:
        m.device_token(c, client_id=client["client_id"], device_code=started["device_code"])
    assert denied.value.error == "access_denied"
