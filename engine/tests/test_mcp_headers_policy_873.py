"""BOSSAPP-873: the header and metadata policy of an MCP server.

  * {} (every server today) sends exactly what it sent before — no X-BOSS-* header, plain static
    headers kept;
  * secret_headers are written to the secret store, resolved at call time, shown as last four;
    a kept one keeps its value, and a caller can never point a header at another secret;
  * allow strips every extra header not on the list, and never the sign-in or protocol headers;
  * forward sends X-BOSS-Run-Id / X-BOSS-Agent (and traceparent when tracing is on) on tools/call
    only — never on initialize, so a server's session is not split per run;
  * names BOSS owns (Authorization, protocol headers, X-BOSS-*) cannot be set by a policy;
  * the routes read and write it.

Needs BOSS_TEST_DATABASE_URL for the end-to-end tests.
"""
from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from boss_engine.services import mcp_headers_policy as hp
from .test_tool_policy_plug_points_815 import eng  # noqa: F401 — fixture


# ── pure ─────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("policy", [
    {"allow": ["Authorization"]}, {"allow": ["Mcp-Session-Id"]}, {"allow": ["X-BOSS-Run-Id"]},
    {"secret_headers": {"Authorization": "x"}}, {"secret_headers": {"bad name": "x"}},
    {"secret_headers": {"X-Key": ""}}, {"forward": ["everything"]}, {"strip": True},
    {"allow": "X-Key"},
])
def test_names_boss_owns_and_malformed_policies_are_refused(policy):
    with pytest.raises(hp.HeadersPolicyError):
        hp.validate(policy)


def test_a_policy_is_normalised():
    assert hp.validate(None) == {} and hp.validate({}) == {}
    got = hp.validate({"allow": ["x-b", "X-A", "x-b"], "forward": ["agent", "run_id"],
                       "secret_headers": {"X-Tenant-Key": "s3cret-value"}})
    assert got == {"allow": ["X-A", "x-b"], "forward": ["run_id", "agent"],
                   "secret_headers": {"X-Tenant-Key": {"secret": "s3cret-value"}}}


def test_call_headers_are_opt_in_and_clean():
    assert hp.call_headers({}, run_id="r1", agent="forge") == {}
    assert hp.call_headers({"forward": ["run_id", "agent"]}, run_id="r1", agent="forge") == {
        "X-BOSS-Run-Id": "r1", "X-BOSS-Agent": "forge"}
    assert hp.call_headers({"forward": ["run_id"]}, run_id="r1\r\nX-Evil: 1") == {}
    # no tracing running: no traceparent is invented
    assert hp.call_headers({"forward": ["traceparent"]}) == {}


def test_the_allowlist_keeps_sign_in_and_protocol_headers():
    headers = {"Authorization": "Bearer t", "X-Api-Key": "k", "X-Other": "1", "X-Keep": "2",
               "Accept": "x"}
    got = hp.apply(None, None, {"allow": ["X-Keep"]}, headers, sign_in={"authorization",
                                                                         "x-api-key"})
    assert got == {"Authorization": "Bearer t", "X-Api-Key": "k", "X-Keep": "2", "Accept": "x"}
    assert hp.apply(None, None, {}, headers, sign_in=set()) == headers  # no policy, no change


# ── end to end, through the receipt door ─────────────────────────────────────────────────────

class HeaderEcho:
    """A tiny MCP server that records each request's method and headers."""

    def __init__(self) -> None:
        self.seen: list = []
        me = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # noqa: D401
                return

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["content-length"])) or b"{}")
                me.seen.append((body.get("method"), {k.lower(): v for k, v in self.headers.items()}))
                if "id" not in body:
                    self.send_response(202)
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                result = ({"protocolVersion": "2025-06-18", "capabilities": {}}
                          if body.get("method") == "initialize"
                          else {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}
                          if body.get("method") == "tools/list"
                          else {"content": [{"type": "text", "text": "ok"}]})
                out = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/mcp"

    def calls(self) -> list[dict]:
        return [h for m, h in self.seen if m == "tools/call"]

    def inits(self) -> list[dict]:
        return [h for m, h in self.seen if m == "initialize"]


@pytest.fixture()
def world(eng):  # noqa: F811
    from boss_engine.config import get_settings
    from boss_engine.services.mcp_client import forget_sessions
    from boss_engine.services.mcp_registry import record_reachability, register_mcp_server

    pid = f"p_873_{uuid.uuid4().hex[:8]}"
    echo = HeaderEcho()
    settings = get_settings()
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_873', 'o') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_873', 'org_873', "
                       "'w') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                       "(:p, 'ws_873', :p, :p)"), {"p": pid})
        server = register_mcp_server(
            c, pid, f"echo-{pid}", echo.url, risk_tier=0, provenance={"trusted": True},
            auth={"type": "none", "headers": {"X-Other": "plain"}}, actor="ceo",
            settings=settings)
        record_reachability(c, pid, server["id"], reachable=True,
                            tools=[{"name": "echo", "inputSchema": {"type": "object"}}])
    forget_sessions()
    yield SimpleNamespace(eng=eng, pid=pid, echo=echo, sid=server["id"], settings=settings)
    echo.server.shutdown()
    echo.server.server_close()


def _call(w, run="run_873", role="forge"):
    from boss_engine.services.mcp_registry import call_tool_with_receipt, get_mcp_server

    with w.eng.begin() as c:
        server = get_mcp_server(c, w.pid, w.sid)
        return call_tool_with_receipt(
            c, w.settings, w.pid, server, "echo", {"x": 1},
            dedupe_key=f"t873:{uuid.uuid4().hex}", tenant_scope=("org_873", "ws_873"),
            approval_about={"run_id": run}, policy_context={"role_key": role, "run_id": run})


def _set(w, policy):
    from boss_engine.services.mcp_registry import get_mcp_server

    with w.eng.begin() as c:
        return hp.set_policy(c, w.settings, w.pid, get_mcp_server(c, w.pid, w.sid), policy,
                             actor="ceo")


def test_no_policy_sends_what_it_sent_before(world):
    w = world
    _call(w)
    sent = w.echo.calls()[-1]
    assert sent["x-other"] == "plain"
    assert not any(k.startswith("x-boss-") for k in sent) and "traceparent" not in sent


def test_secret_headers_allowlist_and_forwarded_metadata(world):
    w = world
    shown = _set(w, {"allow": ["X-Tenant-Key"], "forward": ["run_id", "agent"],
                     "secret_headers": {"X-Tenant-Key": "tenant-secret-0042"}})
    assert shown["secret_headers"] == {"X-Tenant-Key": {"set": True, "last4": "0042"}}
    assert "tenant-secret-0042" not in json.dumps(shown)
    with w.eng.begin() as c:
        stored = c.execute(text("SELECT headers_policy FROM mcp_servers WHERE id = :i"),
                           {"i": w.sid}).scalar()
    assert "tenant-secret-0042" not in json.dumps(stored)  # only the reference is kept
    _call(w, run="run_A", role="scout")
    sent = w.echo.calls()[-1]
    assert sent["x-tenant-key"] == "tenant-secret-0042"
    assert sent["x-boss-run-id"] == "run_A" and sent["x-boss-agent"] == "scout"
    assert "x-other" not in sent  # stripped by the allowlist
    assert all("x-boss-run-id" not in h for h in w.echo.inits())  # never on initialize


def test_a_kept_secret_keeps_its_value_and_cannot_be_swapped(world):
    w = world
    _set(w, {"secret_headers": {"X-Tenant-Key": "first-value-1111"}})
    shown = _set(w, {"secret_headers": {"X-Tenant-Key": {"keep": True}}, "forward": ["run_id"]})
    assert shown["secret_headers"]["X-Tenant-Key"]["last4"] == "1111"
    _call(w)
    assert w.echo.calls()[-1]["x-tenant-key"] == "first-value-1111"
    with pytest.raises(hp.HeadersPolicyError, match="kept, not swapped"):
        _set(w, {"secret_headers": {"X-Tenant-Key": {"secret_ref_id": "sref_someone_else"}}})


def test_the_routes(world):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apps.api.app.routers.mcp_credentials import build_mcp_credentials_router

    w = world
    app = FastAPI()
    app.include_router(build_mcp_credentials_router(
        engine=w.eng, settings=lambda: w.settings,
        require_write_actor=lambda c, p, a, perm: "ceo", require_reader=lambda c, p: None))
    api = TestClient(app)
    url = f"/v1/mcp/servers/{w.sid}/headers-policy"
    empty = api.get(url, params={"project_id": w.pid}).json()
    assert empty["headers_policy"] == {"allow": None, "forward": [], "secret_headers": {}}
    assert [f["header"] for f in empty["forward_choices"]] == ["X-BOSS-Run-Id", "X-BOSS-Agent",
                                                               "traceparent"]
    bad = api.post(url, json={"project_id": w.pid, "headers_policy": {"allow": ["Authorization"]}})
    assert bad.status_code == 422
    ok = api.post(url, json={"project_id": w.pid, "headers_policy": {"forward": ["agent"]}})
    assert ok.status_code == 200 and ok.json()["headers_policy"]["forward"] == ["agent"]
    with w.eng.begin() as c:
        assert c.execute(text("SELECT count(*) FROM events WHERE project_id = :p AND "
                              "event_type = 'mcp.headers_policy_set'"), {"p": w.pid}).scalar() == 1
