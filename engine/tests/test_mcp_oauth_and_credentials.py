"""BOSSAPP-846 + 847: MCP-spec OAuth for remote servers, and whose credential a call carries.

Against a FAKE OAuth + MCP server on a local port (no network beyond 127.0.0.1): discovery
(RFC 9728 resource metadata named by the 401's challenge, RFC 8414 server metadata), dynamic
client registration, the PKCE S256 code flow with a single-use state, tokens kept as vault items
(never in mcp_servers.auth), single-flight refresh, needs_reauth surfacing, the SSRF guard on
every discovered URL, and the credential resolution matrix (shared / per_user / fallback /
per_agent x a person, an agent for a person with and without a lending grant, an agent alone).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine, text

os.environ.setdefault("BOSS_MASTER_KEY", base64.b64encode(b"p29a-test-master-key-32-bytes!!!").decode())

from boss_engine.identity import vault  # noqa: E402
from boss_engine.services import connection_credentials as creds  # noqa: E402
from boss_engine.services import mcp_oauth  # noqa: E402
from boss_engine.services import mcp_registry as reg  # noqa: E402
from boss_engine.services.connection_credentials import CredentialPrincipal  # noqa: E402

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"),
)


# --------------------------------------------------------------------------- #
# The fake provider: an MCP server, its resource metadata and its authorization server
# --------------------------------------------------------------------------- #

class FakeProvider:
    def __init__(self, *, registration: bool = True, s256: bool = True):
        self.registration = registration
        self.s256 = s256
        self.codes: dict[str, dict] = {}          # code -> {challenge, client_id, redirect_uri}
        self.registered: list[dict] = []
        self.token_requests: list[dict] = []
        self.refreshes = 0
        self.revoked_refresh: set[str] = set()
        self.valid_access: set[str] = set()
        self.access_counter = 0
        self.lock = threading.Lock()
        self.refresh_delay = 0.0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.overrides: dict[str, dict] = {}       # path -> json body to answer instead

    def close(self):
        self.server.shutdown()

    def issue_code(self, authorize_url: str) -> tuple[str, str]:
        """What the person's browser does at the provider: approve, get a code back."""
        q = parse_qs(urlsplit(authorize_url).query)
        code = f"code-{uuid.uuid4().hex[:8]}"
        self.codes[code] = {"challenge": q["code_challenge"][0], "client_id": q["client_id"][0],
                            "redirect_uri": q["redirect_uri"][0],
                            "method": q["code_challenge_method"][0]}
        return code, q["state"][0]

    def _mint(self, refresh: bool = True) -> dict:
        with self.lock:
            self.access_counter += 1
            n = self.access_counter
        access = f"at-{n}"
        self.valid_access.add(access)
        body = {"access_token": access, "token_type": "Bearer", "expires_in": 3600,
                "scope": "read write"}
        if refresh:
            body["refresh_token"] = f"rt-{n}"
        claims = base64.urlsafe_b64encode(json.dumps({"email": "alice@example.com"}).encode()) \
            .rstrip(b"=").decode()
        body["id_token"] = f"e30.{claims}.sig"
        return body

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401
                pass

            def _send(self, status, body=None, headers=None):
                raw = json.dumps(body).encode() if body is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def do_GET(self):
                path = urlsplit(self.path).path
                if path in fake.overrides:
                    return self._send(200, fake.overrides[path])
                if path == "/.well-known/oauth-protected-resource/mcp":
                    return self._send(200, {"resource": fake.base + "/mcp",
                                            "authorization_servers": [fake.base + "/as"],
                                            "scopes_supported": ["read", "write"]})
                if path == "/.well-known/oauth-authorization-server/as":
                    meta = {"issuer": fake.base + "/as",
                            "authorization_endpoint": fake.base + "/as/authorize",
                            "token_endpoint": fake.base + "/as/token",
                            "code_challenge_methods_supported": ["S256"] if fake.s256 else ["plain"],
                            "token_endpoint_auth_methods_supported": ["none"]}
                    if fake.registration:
                        meta["registration_endpoint"] = fake.base + "/as/register"
                    return self._send(200, meta)
                return self._send(404, {"error": "not_found"})

            def do_POST(self):
                path = urlsplit(self.path).path
                raw = self._body()
                if path == "/mcp":
                    auth = self.headers.get("Authorization") or ""
                    token = auth.removeprefix("Bearer ").strip()
                    if token not in fake.valid_access:
                        return self._send(401, {"error": "unauthorized"}, {
                            "WWW-Authenticate": f'Bearer resource_metadata="{fake.base}'
                                                f'/.well-known/oauth-protected-resource/mcp", '
                                                f'scope="read write"'})
                    msg = json.loads(raw or b"{}")
                    if msg.get("method") == "tools/list":
                        return self._send(200, {"jsonrpc": "2.0", "id": msg.get("id"),
                                                "result": {"tools": [{"name": "whoami",
                                                                      "inputSchema": {}}]}})
                    return self._send(200, {"jsonrpc": "2.0", "id": msg.get("id"),
                                            "result": {"protocolVersion": "2025-06-18"}})
                if path == "/as/register":
                    req = json.loads(raw)
                    fake.registered.append(req)
                    return self._send(201, {"client_id": f"cid-{len(fake.registered)}",
                                            "token_endpoint_auth_method": "none"})
                if path == "/as/token":
                    form = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
                    fake.token_requests.append(form)
                    if form.get("grant_type") == "authorization_code":
                        grant = fake.codes.pop(form.get("code", ""), None)
                        verifier = form.get("code_verifier", "")
                        digest = base64.urlsafe_b64encode(
                            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                        if (grant is None or grant["method"] != "S256"
                                or digest != grant["challenge"]
                                or grant["redirect_uri"] != form.get("redirect_uri")
                                or grant["client_id"] != form.get("client_id")):
                            return self._send(400, {"error": "invalid_grant"})
                        return self._send(200, fake._mint())
                    if form.get("grant_type") == "refresh_token":
                        import time as _t

                        if fake.refresh_delay:
                            _t.sleep(fake.refresh_delay)
                        with fake.lock:
                            fake.refreshes += 1
                        if form.get("refresh_token") in fake.revoked_refresh:
                            return self._send(400, {"error": "invalid_grant"})
                        return self._send(200, fake._mint())
                    return self._send(400, {"error": "unsupported_grant_type"})
                return self._send(404, {"error": "not_found"})

        return H


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT credential_policy, auth_status FROM mcp_servers LIMIT 1"))
            c.execute(text("SELECT 1 FROM connection_credentials LIMIT 1"))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"test database unavailable or not migrated: {error}")
    vault.reset_master_key_cache()
    return engine


@pytest.fixture(autouse=True)
def _local_provider_allowed(monkeypatch):
    """The install opts the fake's host in (BOSS_OUTBOUND_ALLOW_PRIVATE), as an operator would."""
    from boss_engine.identity import http as safe_http

    monkeypatch.setattr(safe_http, "_allowed_private_hosts", lambda: frozenset({"127.0.0.1"}))


@pytest.fixture()
def fake():
    provider = FakeProvider()
    yield provider
    provider.close()


@pytest.fixture()
def project(eng):
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT id FROM workspaces LIMIT 1")).scalar()
        if wid is None:
            pytest.skip("test database has no workspace")
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'p29a test', :s)"), {"i": pid, "w": wid, "s": pid})
    yield pid
    with eng.begin() as c:
        items = list(c.execute(text("SELECT vault_item_id FROM connection_credentials WHERE "
                                    "project_id = :p"), {"p": pid}).scalars().all())
        items += list(c.execute(text(
            "SELECT v.id FROM identity_vault_items v JOIN mcp_servers s ON v.provider = "
            "'mcp:' || s.id WHERE s.project_id = :p"), {"p": pid}).scalars().all())
        for table in ("connection_credentials", "mcp_oauth_pending", "mcp_access_grants",
                      "role_mcp_servers", "roles", "tool_call_audit", "receipts", "mcp_servers",
                      "events", "decisions", "project_members", "project_configurations",
                      "secret_refs"):
            try:
                with c.begin_nested():
                    c.execute(text(f"DELETE FROM {table} WHERE project_id=:p"), {"p": pid})
            except Exception:  # noqa: BLE001
                pass
        if items:
            c.execute(text("DELETE FROM identity_vault_items WHERE id = ANY(:i)"), {"i": items})
        c.execute(text("DELETE FROM projects WHERE id=:p"), {"p": pid})


def _oauth_server(eng, project, fake, *, name="linear", policy="shared"):
    with eng.begin() as c:
        server = reg.register_mcp_server(c, project, name, fake.base + "/mcp", risk_tier=0,
                                         actor="operator", auth={"type": "oauth"})
        if policy != "shared":
            creds.set_policy(c, project, server["id"], policy, actor="operator")
    return server["id"]


def _sign_in(eng, project, fake, sid, *, subject_kind="org", subject_id=None):
    redirect = "https://boss.example.test" + mcp_oauth.CALLBACK_PATH
    with eng.begin() as c:
        subject_id = subject_id or creds.org_of_project(c, project)
    started = mcp_oauth.begin_sign_in(eng.begin, None, project, sid, subject_kind=subject_kind,
                                      subject_id=subject_id, redirect_uri=redirect,
                                      actor="operator")
    code, state = fake.issue_code(started["redirect"])
    assert state == started["state"]
    return mcp_oauth.complete(eng.begin, None, state=state, code=code), started


def _server(eng, project, sid):
    with eng.begin() as c:
        return reg.get_mcp_server(c, project, sid)


def _headers(eng, project, sid, principal=None):
    with eng.begin() as c:
        server = reg.get_mcp_server(c, project, sid)
        return reg.auth_headers(c, None, project, server, principal=principal)


# --------------------------------------------------------------------------- #
# 846: discovery, registration, PKCE, storage
# --------------------------------------------------------------------------- #

def test_discovery_follows_the_401_challenge_to_both_metadata_documents(fake):
    meta = mcp_oauth.discover(fake.base + "/mcp")
    assert meta["resource"] == fake.base + "/mcp"
    assert meta["authorization_server"] == fake.base + "/as"
    assert meta["authorization_endpoint"] == fake.base + "/as/authorize"
    assert meta["token_endpoint"] == fake.base + "/as/token"
    assert meta["registration_endpoint"] == fake.base + "/as/register"
    assert meta["scope"] == "read write"


def test_sign_in_registers_a_client_runs_pkce_and_keeps_tokens_only_in_the_vault(eng, project,
                                                                                 fake):
    sid = _oauth_server(eng, project, fake)
    assert _server(eng, project, sid)["auth_status"] == "not_connected"
    done, started = _sign_in(eng, project, fake, sid)
    # Dynamic registration: a public client with exactly the callback.
    [registered] = fake.registered
    assert registered["redirect_uris"] == ["https://boss.example.test/v1/mcp/oauth/callback"]
    assert registered["token_endpoint_auth_method"] == "none"
    # PKCE S256 and the resource indicator went to the provider; the code exchange carried the
    # verifier (the fake refuses a mismatch), never a client secret.
    q = parse_qs(urlsplit(started["redirect"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["resource"] == [fake.base + "/mcp"]
    exchange = fake.token_requests[-1]
    assert exchange["grant_type"] == "authorization_code" and exchange["code_verifier"]
    assert "client_secret" not in exchange
    assert done["credential"]["subject_kind"] == "org"
    assert done["credential"]["connected_as"] == "alice@example.com"
    server = _server(eng, project, sid)
    assert server["auth_status"] == "connected"
    assert server["auth"]["type"] == "oauth"
    assert server["auth"]["oauth"]["client_id"] == "cid-1"
    with eng.begin() as c:
        raw = c.execute(text("SELECT auth::text FROM mcp_servers WHERE id = :i"), {"i": sid}).scalar()
        item = c.execute(text("SELECT v.kind, v.provider, v.owner_principal_id FROM "
                              "connection_credentials cc JOIN identity_vault_items v ON "
                              "v.id = cc.vault_item_id WHERE cc.server_id = :s"),
                         {"s": sid}).mappings().one()
        pending = c.execute(text("SELECT count(*) FROM mcp_oauth_pending WHERE server_id = :s"),
                            {"s": sid}).scalar()
    assert "at-1" not in raw and "rt-1" not in raw                 # never in the config
    assert item["kind"] == "oauth_user" and item["provider"] == f"mcp:{sid}"
    assert item["owner_principal_id"] is None                       # the org's, not a person's
    assert pending == 0                                             # the state was spent
    # The call carries the vault's token, and the fake MCP server accepts it.
    headers = _headers(eng, project, sid)
    assert headers["Authorization"] == "Bearer at-1"
    from boss_engine.services.mcp_client import MCPClient, forget_sessions

    forget_sessions()
    tools = MCPClient(None).list_tools(fake.base + "/mcp", headers=headers)
    assert [t["name"] for t in tools] == ["whoami"]


def test_the_state_is_single_use_and_only_its_hash_is_kept(eng, project, fake):
    sid = _oauth_server(eng, project, fake)
    with eng.begin() as c:
        org = creds.org_of_project(c, project)
    started = mcp_oauth.begin_sign_in(eng.begin, None, project, sid, subject_kind="org",
                                      subject_id=org, redirect_uri="https://b.test/cb",
                                      actor="operator")
    with eng.begin() as c:
        stored = c.execute(text("SELECT state_hash FROM mcp_oauth_pending WHERE server_id = :s"),
                           {"s": sid}).scalar()
    assert stored != started["state"] and stored == hashlib.sha256(
        started["state"].encode()).hexdigest()
    code, state = fake.issue_code(started["redirect"])
    mcp_oauth.complete(eng.begin, None, state=state, code=code)
    with pytest.raises(mcp_oauth.McpOAuthError) as replay:
        mcp_oauth.complete(eng.begin, None, state=state, code=code)
    assert replay.value.code == "invalid_state"
    with pytest.raises(mcp_oauth.McpOAuthError) as forged:
        mcp_oauth.complete(eng.begin, None, state="not-one-boss-made", code="x")
    assert forged.value.code == "invalid_state"


def test_an_expired_or_denied_sign_in_spends_the_state_and_stores_nothing(eng, project, fake):
    sid = _oauth_server(eng, project, fake)
    with eng.begin() as c:
        org = creds.org_of_project(c, project)
    started = mcp_oauth.begin_sign_in(eng.begin, None, project, sid, subject_kind="org",
                                      subject_id=org, redirect_uri="https://b.test/cb",
                                      actor="operator")
    with eng.begin() as c:
        c.execute(text("UPDATE mcp_oauth_pending SET expires_at = now() - interval '1 minute' "
                       "WHERE server_id = :s"), {"s": sid})
    with pytest.raises(mcp_oauth.McpOAuthError) as late:
        mcp_oauth.complete(eng.begin, None, state=started["state"], code="c")
    assert late.value.code == "expired"
    again = mcp_oauth.begin_sign_in(eng.begin, None, project, sid, subject_kind="org",
                                    subject_id=org, redirect_uri="https://b.test/cb",
                                    actor="operator")
    with pytest.raises(mcp_oauth.McpOAuthError) as denied:
        mcp_oauth.complete(eng.begin, None, state=again["state"], error="access_denied")
    assert denied.value.code == "denied"
    with eng.begin() as c:
        assert creds.list_credentials(c, project, server_id=sid) == []
    assert len(fake.registered) == 1          # the registered client was reused, not re-made


def test_without_registration_a_typed_client_is_needed_and_then_used(eng, project, fake):
    fake.registration = False
    sid = _oauth_server(eng, project, fake)
    with pytest.raises(mcp_oauth.NeedsClient):
        _sign_in(eng, project, fake, sid)
    with eng.begin() as c:
        reg.update_mcp_server(c, project, sid, actor="operator", auth={"type": "oauth", "oauth": {
            "client_id": "typed-client"}})
    done, started = _sign_in(eng, project, fake, sid)
    assert parse_qs(urlsplit(started["redirect"]).query)["client_id"] == ["typed-client"]
    assert fake.registered == []
    assert _server(eng, project, sid)["auth"]["oauth"]["client_source"] == "manual"
    assert done["credential"]["status"] == "active"


def test_an_authorization_server_without_s256_is_refused(fake):
    fake.s256 = False
    with pytest.raises(mcp_oauth.McpOAuthError) as refused:
        mcp_oauth.discover(fake.base + "/mcp")
    assert refused.value.code == "no_pkce"


# --------------------------------------------------------------------------- #
# 846: refresh, single flight, needs_reauth
# --------------------------------------------------------------------------- #

def _expire_soon(eng, sid, *, minutes=1):
    with eng.begin() as c:
        c.execute(text("UPDATE identity_vault_items SET access_expires_at = now() + "
                       "make_interval(mins => :m) WHERE id = (SELECT vault_item_id FROM "
                       "connection_credentials WHERE server_id = :s)"), {"s": sid, "m": minutes})


def test_a_token_near_expiry_is_refreshed_once_for_concurrent_callers(eng, project, fake):
    sid = _oauth_server(eng, project, fake)
    _sign_in(eng, project, fake, sid)
    _expire_soon(eng, sid)
    fake.refresh_delay = 0.3
    results: list[str] = []
    errors: list[BaseException] = []

    def call():
        try:
            results.append(_headers(eng, project, sid)["Authorization"])
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=call) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert fake.refreshes == 1                                     # single flight
    assert set(results) == {"Bearer at-2"}
    # The rotated refresh token was kept: the next refresh presents rt-2.
    _expire_soon(eng, sid)
    fake.refresh_delay = 0
    assert _headers(eng, project, sid)["Authorization"] == "Bearer at-3"
    assert fake.token_requests[-1]["refresh_token"] == "rt-2"


def test_a_refused_refresh_leaves_the_server_needing_reconnect(eng, project, fake):
    sid = _oauth_server(eng, project, fake)
    _sign_in(eng, project, fake, sid)
    fake.revoked_refresh.add("rt-1")
    _expire_soon(eng, sid)
    with pytest.raises(creds.CredentialNeedsReauth):
        _headers(eng, project, sid)
    server = _server(eng, project, sid)
    assert server["auth_status"] == "needs_reauth"
    with eng.begin() as c:
        [row] = creds.list_credentials(c, project, server_id=sid)
    assert row["status"] == "needs_reauth"
    # It is not retried in a loop: the next call refuses without asking the provider again.
    before = fake.refreshes
    with pytest.raises(creds.CredentialNeedsReauth):
        _headers(eng, project, sid)
    assert fake.refreshes == before
    # Reconnect clears it.
    _sign_in(eng, project, fake, sid)
    assert _server(eng, project, sid)["auth_status"] == "connected"
    assert _headers(eng, project, sid)["Authorization"].startswith("Bearer at-")


def test_an_expired_token_with_no_refresh_needs_reauth(eng, project, fake):
    sid = _oauth_server(eng, project, fake)
    _sign_in(eng, project, fake, sid)
    with eng.begin() as c:
        c.execute(text("UPDATE identity_vault_items SET refresh_enc = NULL, refresh_nonce = NULL, "
                       "access_expires_at = now() - interval '1 minute' WHERE id = (SELECT "
                       "vault_item_id FROM connection_credentials WHERE server_id = :s)"),
                  {"s": sid})
    with pytest.raises(creds.CredentialNeedsReauth):
        _headers(eng, project, sid)
    assert _server(eng, project, sid)["auth_status"] == "needs_reauth"


# --------------------------------------------------------------------------- #
# 846: the SSRF guard
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("url,code", [
    ("http://mcp.example.com/mcp", "https_required"),
    ("https://10.0.0.5/token", "private_destination"),
    ("https://169.254.169.254/latest/meta-data", "private_destination"),
    ("https://[::1]/x", "private_destination"),
    ("https://100.64.1.1/x", "private_destination"),
    ("https://user:pw@mcp.example.com/x", "credentials_in_url"),
    ("file:///etc/passwd", "bad_url"),
])
def test_the_guard_refuses_unsafe_urls(monkeypatch, url, code):
    from boss_engine.identity import http as safe_http

    monkeypatch.setattr(safe_http, "_allowed_private_hosts", lambda: frozenset())
    monkeypatch.setattr(safe_http, "_resolve", lambda host, port: ["93.184.216.34"])
    with pytest.raises(mcp_oauth.UnsafeDestination) as refused:
        mcp_oauth.guard_url(url, what="token endpoint")
    assert refused.value.code == code


def test_the_guard_refuses_a_name_that_resolves_to_a_private_address(monkeypatch):
    from boss_engine.identity import http as safe_http

    monkeypatch.setattr(safe_http, "_allowed_private_hosts", lambda: frozenset())
    monkeypatch.setattr(safe_http, "_resolve", lambda host, port: ["93.184.216.34", "10.1.2.3"])
    with pytest.raises(mcp_oauth.UnsafeDestination):
        mcp_oauth.guard_url("https://rebind.example.com/token", what="token endpoint")
    monkeypatch.setattr(safe_http, "_resolve", lambda host, port: ["93.184.216.34"])
    assert mcp_oauth.guard_url("https://ok.example.com/token", what="x")


def test_the_local_provider_is_refused_unless_the_install_opts_in(monkeypatch, fake):
    from boss_engine.identity import http as safe_http

    monkeypatch.setattr(safe_http, "_allowed_private_hosts", lambda: frozenset())
    with pytest.raises(mcp_oauth.UnsafeDestination):
        mcp_oauth.discover(fake.base + "/mcp")


def test_discovered_endpoints_pointing_somewhere_private_are_refused(fake):
    fake.overrides["/.well-known/oauth-authorization-server/as"] = {
        "issuer": fake.base + "/as", "authorization_endpoint": fake.base + "/as/authorize",
        "token_endpoint": "https://169.254.169.254/token",
        "code_challenge_methods_supported": ["S256"]}
    with pytest.raises(mcp_oauth.UnsafeDestination) as refused:
        mcp_oauth.discover(fake.base + "/mcp")
    assert "token endpoint" in refused.value.message


def test_metadata_about_another_resource_or_issuer_is_refused(fake):
    fake.overrides["/.well-known/oauth-protected-resource/mcp"] = {
        "resource": "https://elsewhere.example.com/mcp",
        "authorization_servers": [fake.base + "/as"]}
    with pytest.raises(mcp_oauth.McpOAuthError) as other_resource:
        mcp_oauth.discover(fake.base + "/mcp")
    assert other_resource.value.code == "resource_mismatch"
    del fake.overrides["/.well-known/oauth-protected-resource/mcp"]
    fake.overrides["/.well-known/oauth-authorization-server/as"] = {
        "issuer": "https://evil.example.com", "authorization_endpoint": fake.base + "/a",
        "token_endpoint": fake.base + "/t"}
    with pytest.raises(mcp_oauth.McpOAuthError) as other_issuer:
        mcp_oauth.discover(fake.base + "/mcp")
    assert other_issuer.value.code == "issuer_mismatch"


# --------------------------------------------------------------------------- #
# 847: the resolution matrix and delegation
# --------------------------------------------------------------------------- #

PERSON = "usr_p29a_alice"
OTHER = "usr_p29a_bob"


@pytest.fixture()
def people(eng, project):
    with eng.begin() as c:
        org = creds.org_of_project(c, project)
        for pid in (PERSON, OTHER):
            c.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) "
                           "VALUES (:i, :o, 'user', 'active', :i) ON CONFLICT (id) DO NOTHING"),
                      {"i": pid, "o": org})
    grants: list[str] = []

    def grant(person=PERSON, resources=None, status="active"):
        gid = f"dlg_{uuid.uuid4().hex[:12]}"
        with eng.begin() as c:
            c.execute(text(
                "INSERT INTO identity_delegation_grants (id, org_id, user_principal_id, agent_ids, "
                "project_ids, resources, status, expires_at) VALUES (:i, :o, :u, :a, :p, "
                "CAST(:r AS JSONB), :st, now() + interval '1 day')"),
                {"i": gid, "o": org, "u": person, "a": ["agt_p29a"], "p": [project],
                 "r": json.dumps(resources or {}), "st": status})
        grants.append(gid)
        return gid

    yield grant
    with eng.begin() as c:
        c.execute(text("DELETE FROM identity_delegation_grants WHERE id = ANY(:g)"), {"g": grants})


def _static_server(eng, project, policy, *, name="tracker"):
    with eng.begin() as c:
        server = reg.register_mcp_server(c, project, name, "http://tracker:9/mcp", risk_tier=0,
                                         actor="operator",
                                         auth={"type": "bearer", "secret": "shared-token-0001"})
        creds.set_policy(c, project, server["id"], policy, actor="operator")
    return server["id"]


def _give(eng, project, sid, kind, subject, token):
    with eng.begin() as c:
        creds.store(c, project, server_id=sid, subject_kind=kind, subject_id=subject,
                    secret=token, kind="token", actor="operator")


@pytest.fixture()
def secret_store(monkeypatch):
    """The shared bearer token lives in the secret store; tests read it back from memory."""
    stored: dict[str, str] = {}
    from boss_engine.services import secret_backends, secrets_service

    def put(connection, settings, *, project_id, name, value, **_):
        ref = f"sref_{uuid.uuid4().hex[:10]}"
        stored[ref] = value
        return {"secret_ref_id": ref, "last4": value[-4:]}

    monkeypatch.setattr(secrets_service, "put_secret", put)
    monkeypatch.setattr(secret_backends, "default_secret_backend", lambda settings: "memory")
    monkeypatch.setattr(secret_backends, "resolve_secret_ref",
                        lambda connection, ref, settings, project_id=None: stored[ref])
    return stored


def test_the_resolution_matrix(eng, project, people, secret_store):
    lend = people(resources={"mcp": {"as_user": True}})
    no_lend = people(resources={"github": {"as_user": True}})
    person = CredentialPrincipal(person_id=PERSON)
    agent_lent = CredentialPrincipal(person_id=PERSON, agent_role="pm", agent_id="agt_p29a",
                                     grant_id=lend)
    agent_not_lent = CredentialPrincipal(person_id=PERSON, agent_role="pm", agent_id="agt_p29a",
                                         grant_id=no_lend)
    agent_alone = CredentialPrincipal(agent_role="pm")
    nobody = CredentialPrincipal()
    shared, mine, agents = ("Bearer shared-token-0001", "Bearer alice-token-0001",
                            "Bearer pm-token-0001")
    unavailable = creds.CredentialUnavailable
    expected = {
        "shared": {"person": shared, "agent_lent": shared, "agent_not_lent": shared,
                   "agent_alone": shared, "nobody": shared},
        "per_user": {"person": mine, "agent_lent": mine, "agent_not_lent": unavailable,
                     "agent_alone": unavailable, "nobody": unavailable},
        "per_user_fallback": {"person": mine, "agent_lent": mine, "agent_not_lent": shared,
                              "agent_alone": shared, "nobody": shared},
        "per_agent": {"person": unavailable, "agent_lent": agents, "agent_not_lent": agents,
                      "agent_alone": agents, "nobody": unavailable},
    }
    principals = {"person": person, "agent_lent": agent_lent, "agent_not_lent": agent_not_lent,
                  "agent_alone": agent_alone, "nobody": nobody}
    for policy, row in expected.items():
        sid = _static_server(eng, project, policy, name=f"tracker-{policy.replace('_', '-')}")
        _give(eng, project, sid, "user", PERSON, "alice-token-0001")
        _give(eng, project, sid, "agent", "pm", "pm-token-0001")
        for who, want in row.items():
            if isinstance(want, str):
                got = _headers(eng, project, sid, principals[who])
                assert got.get("Authorization") == want, (policy, who)
            else:
                with pytest.raises(want):
                    _headers(eng, project, sid, principals[who])


def test_fallback_uses_the_shared_one_when_the_person_has_none(eng, project, people, secret_store):
    sid = _static_server(eng, project, "per_user_fallback")
    got = _headers(eng, project, sid, CredentialPrincipal(person_id=OTHER))
    assert got["Authorization"] == "Bearer shared-token-0001"
    with pytest.raises(creds.CredentialUnavailable):
        with eng.begin() as c:
            creds.set_policy(c, project, sid, "per_user", actor="operator")
        _headers(eng, project, sid, CredentialPrincipal(person_id=OTHER))


def test_a_lending_grant_must_be_live_the_persons_own_and_name_this_server(eng, project, people,
                                                                          secret_store):
    sid = _static_server(eng, project, "per_user")
    _give(eng, project, sid, "user", PERSON, "alice-token-0001")
    other_server = people(resources={"mcp": {"as_user": True, "servers": ["mcps_nope"]}})
    this_server = people(resources={"mcp": {"as_user": True, "servers": [sid]}})
    narrow = people(resources={f"mcp:{sid}": {"as_user": True}})
    revoked = people(resources={"mcp": {"as_user": True}}, status="revoked")
    bobs = people(person=OTHER, resources={"mcp": {"as_user": True}})

    def agent(grant):
        return CredentialPrincipal(person_id=PERSON, agent_role="pm", grant_id=grant)

    for good in (this_server, narrow):
        assert _headers(eng, project, sid, agent(good))["Authorization"] == \
            "Bearer alice-token-0001"
    for bad in (other_server, revoked, bobs, None):
        with pytest.raises(creds.CredentialUnavailable):
            _headers(eng, project, sid, agent(bad))


def test_the_principal_comes_from_the_run_context_never_a_field(eng, project, people,
                                                                 secret_store):
    from boss_engine.identity import lineage
    from boss_engine.identity.principal import (
        ActorChain,
        AgentInstance,
        AuthContext,
        AuthMethod,
        Principal,
        PrincipalKind,
        PrincipalStatus,
        RunMode,
        RunTokenClaims,
        TaskBinding,
    )

    lend = people(resources={"mcp": {"as_user": True}})
    sid = _static_server(eng, project, "per_user")
    _give(eng, project, sid, "user", PERSON, "alice-token-0001")
    with eng.begin() as c:
        org = creds.org_of_project(c, project)
    alice = Principal(id=PERSON, kind=PrincipalKind.USER, org_id=org,
                      status=PrincipalStatus.ACTIVE, display_name="Alice")
    claims = RunTokenClaims(
        iss="boss", aud="boss:api", sub=PERSON, org_id=org, mode=RunMode.OBO,
        chain=ActorChain((AgentInstance("agt_p29a", "run_1"),)), grant_id=lend,
        task=TaskBinding(run_id="run_1", project_id=project), scope=frozenset(), cnf_jkt="x",
        jti="j", parent_jti=None, iat=0, exp=0)
    method = next(iter(AuthMethod))
    token = lineage._CONTEXT.set(AuthContext(principal=alice, method=method, run_token=claims))
    try:
        assert _headers(eng, project, sid)["Authorization"] == "Bearer alice-token-0001"
        with eng.begin() as c:
            who = creds.principal_from_context(c, project)
        assert who.person_id == PERSON and who.agent_id == "agt_p29a" and who.grant_id == lend
    finally:
        lineage._CONTEXT.reset(token)
    # Outside any context, an agent named by the run (acting_agent) has no person to lend.
    with creds.acting_agent("pm"):
        with pytest.raises(creds.CredentialUnavailable):
            _headers(eng, project, sid)


def test_a_person_signs_in_with_their_own_account(eng, project, fake, people):
    sid = _oauth_server(eng, project, fake, policy="per_user")
    with pytest.raises(creds.CredentialUnavailable):
        _headers(eng, project, sid, CredentialPrincipal(person_id=PERSON))
    _sign_in(eng, project, fake, sid, subject_kind="user", subject_id=PERSON)
    with eng.begin() as c:
        owner = c.execute(text("SELECT v.owner_principal_id FROM connection_credentials cc JOIN "
                               "identity_vault_items v ON v.id = cc.vault_item_id WHERE "
                               "cc.server_id = :s"), {"s": sid}).scalar()
    assert owner == PERSON                                  # the vault item is the person's
    assert _headers(eng, project, sid, CredentialPrincipal(person_id=PERSON))[
        "Authorization"].startswith("Bearer at-")
    with pytest.raises(creds.CredentialUnavailable):
        _headers(eng, project, sid, CredentialPrincipal(person_id=OTHER))
    # A shared OAuth server with nobody signed in says so.
    shared = _oauth_server(eng, project, fake, name="notion")
    with pytest.raises(creds.CredentialUnavailable) as none_yet:
        _headers(eng, project, shared)
    assert "Sign in" in str(none_yet.value)


def test_a_reconnect_replaces_and_shreds_the_old_token(eng, project, people, secret_store):
    sid = _static_server(eng, project, "per_user")
    _give(eng, project, sid, "user", PERSON, "alice-token-0001")
    with eng.begin() as c:
        first = c.execute(text("SELECT vault_item_id FROM connection_credentials WHERE "
                               "server_id = :s"), {"s": sid}).scalar()
    _give(eng, project, sid, "user", PERSON, "alice-token-0002")
    with eng.begin() as c:
        rows = creds.list_credentials(c, project, server_id=sid)
        old = c.execute(text("SELECT status FROM identity_vault_items WHERE id = :i"),
                        {"i": first}).scalar()
    assert len(rows) == 1 and old == "revoked"
    assert _headers(eng, project, sid, CredentialPrincipal(person_id=PERSON))[
        "Authorization"] == "Bearer alice-token-0002"
    with eng.begin() as c:
        creds.remove(c, project, rows[0]["id"], actor="operator")
        assert creds.list_credentials(c, project, server_id=sid) == []


def test_a_kit_connection_follows_its_credential_policy(eng, project, people, secret_store):
    from boss_engine.services import kit_connections as kc

    with eng.begin() as c:
        kc._put_config(c, project, kc.WEB_SEARCH_KEY, {"provider": "exa",
                                                       "secret_ref_id": "sref_shared"}, "op")
    secret_store["sref_shared"] = "shared-search-key"
    with eng.begin() as c:
        assert kc.search_settings(c, None, project)["key"] == "shared-search-key"
        creds.set_kit_policy(c, project, "web_search", "per_user_fallback", actor="op")
        creds.store(c, project, connection_id=creds.kit_connection_id("web_search"),
                    subject_kind="user", subject_id=PERSON, secret="alice-search-key",
                    kind="token", actor="op")
    with eng.begin() as c:
        token = lineage_person(c, project, PERSON)
        assert token == "alice-search-key"
        assert kc.search_settings(c, None, project)["key"] == "shared-search-key"  # no person
        assert kc.search_settings(c, None, project, shared_only=True)["key"] == "shared-search-key"


def lineage_person(connection, project, person):
    from boss_engine.services import kit_connections as kc

    body = kc._config(connection, project, kc.WEB_SEARCH_KEY)
    return creds.kit_secret(connection, None, project, "web_search",
                            policy=body["credential_policy"], shared="shared-search-key",
                            principal=CredentialPrincipal(person_id=person))


def test_the_server_row_and_public_auth_never_show_a_secret(eng, project, fake):
    with eng.begin() as c:
        server = reg.register_mcp_server(c, project, "sentry", fake.base + "/mcp", risk_tier=0,
                                         actor="operator", auth={"type": "oauth", "oauth": {
                                             "client_id": "typed"}, "client_secret": "s3cr3t-value"})
    shown = json.dumps(server)
    assert "s3cr3t-value" not in shown
    assert server["auth"]["oauth"] == {"client_id": "typed", "client_source": "manual"}
    assert server["credential_policy"] == "shared" and server["auth_status"] == "not_connected"
