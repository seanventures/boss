"""BOSSAPP-815/817 against a real database: the three plug points agree, simulate, the API, the
"Always allow" lifecycle, and the schema block's idempotency.

  * THE THREE PLUG POINTS AGREE — mcp_gateway.tool_policy_verdict, the receipt door
    (mcp_registry.call_tool_with_receipt) and the kernel's RegistryToolSet.invoke answer the same
    call the same way, because each asks tool_policy_engine.decide;
  * the receipt door's audit row carries the REAL matched ids (a rule id, not "allow") and, when a
    rule decided, its explanation under screen.policy;
  * the gateway: a deny rule answers a typed error with no card; an allow rule lets a tier-1 Ask
    tool run with no card; an ask rule parks a tier-0 tool;
  * simulate decides as the real call would and records nothing; the /v1/tool-policies routes;
  * ALWAYS ALLOW (817): approve the Ask card with always_allow -> a source=decision rule ->
    the next call runs without asking and counts a hit (mcp.always_allow_used) -> the tool's
    schema changes (818) -> the rule is revoked, audited -> revoking by hand; refused on a tier-2
    tool and on a card that is not about a tool call (the approval refused with it);
  * the schema block applies twice without error.

Needs BOSS_TEST_DATABASE_URL naming a disposable test database (full schema is applied).
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import mcp_gateway
from boss_engine.services import tool_policy_engine as tpe

ROOT = Path(__file__).resolve().parents[2]
PG_URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")


class FakeMcp:
    TOOLS = [
        {"name": "echo", "description": "Echo.",
         "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
        {"name": "note", "description": "Tier 1, set to Ask.",
         "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}},
        {"name": "write", "description": "Tier 2.", "inputSchema": {"type": "object"}},
    ]

    def __init__(self) -> None:
        self.calls: list = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # noqa: D401
                return

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["content-length"])) or b"{}")
                if "id" not in body:
                    self.send_response(202)
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                method, params = body.get("method"), body.get("params") or {}
                if method == "initialize":
                    result = {"protocolVersion": params.get("protocolVersion"),
                              "capabilities": {"tools": {}},
                              "serverInfo": {"name": "fake", "version": "1"}}
                elif method == "tools/list":
                    result = {"tools": FakeMcp.TOOLS}
                elif method == "tools/call":
                    fake.calls.append((params["name"], params.get("arguments") or {}))
                    result = {"content": [{"type": "text", "text": "done"}]}
                else:
                    result = {}
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

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(scope="module")
def eng():
    if not PG_URL.startswith("postgres") or "test" not in PG_URL.rsplit("/", 1)[-1]:
        pytest.skip("needs BOSS_TEST_DATABASE_URL naming a disposable test database")
    from boss_engine.db import ensure_schema

    engine = create_engine(PG_URL, future=True)
    ensure_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def world(eng):
    from boss_engine.config import get_settings
    from boss_engine.services.mcp_registry import record_reachability, register_mcp_server

    pid = f"p_815_{uuid.uuid4().hex[:8]}"
    fake = FakeMcp()
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_815', 'o') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_815', 'org_815', "
                       "'w') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                       "(:p, 'ws_815', :p, :p)"), {"p": pid})
        c.execute(text(
            "INSERT INTO project_members (id, project_id, member_id, display_name, member_type, "
            "role_keys, permissions) VALUES (:i, :p, 'ceo', 'ceo', 'human', '[\"ceo\"]'::jsonb, "
            "'[\"decision_resolve\"]'::jsonb)"), {"i": f"pm_{pid}", "p": pid})
        server = register_mcp_server(
            c, pid, f"fake-{pid}", fake.url, risk_tier=1, provenance={"trusted": True},
            tool_risk_overrides={"write": 2}, tool_policy={"note": "ask"}, actor="ceo")
        record_reachability(c, pid, server["id"], reachable=True, tools=FakeMcp.TOOLS)
    settings = get_settings()
    yield SimpleNamespace(eng=eng, pid=pid, fake=fake, settings=settings, sid=server["id"],
                          name=server["name"])
    fake.stop()


def _server(w):
    from boss_engine.services.mcp_registry import get_mcp_server

    with w.eng.begin() as c:
        return get_mcp_server(c, w.pid, w.sid)


def _rule(w, **fields):
    with w.eng.begin() as c:
        return tpe.create_rule(c, w.pid, {"scope": "project", "priority": 100, **fields},
                               actor="ceo")


def _call(w, tool, args=None, *, approved_by=None, role="forge"):
    """The receipt door, as every road reaches it. A refusal is raised AFTER the transaction
    commits, so its audit row is kept (a caller that rolls back loses it, as it always did)."""
    from boss_engine.services.mcp_registry import McpServerError, call_tool_with_receipt

    refused = None
    with w.eng.begin() as c:
        try:
            return call_tool_with_receipt(
                c, w.settings, w.pid, _server(w), tool, args or {},
                dedupe_key=f"t815:{uuid.uuid4().hex}", tenant_scope=("org_815", "ws_815"),
                approved_by=approved_by, approval_about={"run_id": "run_815"},
                policy_context={"role_key": role, "run_id": "run_815"})
        except McpServerError as error:
            refused = error
    raise refused


def _audits(w, tool):
    with w.eng.begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT decision, matched_policy_ids, outcome, screen, principal, agent FROM tool_call_audit "
            "WHERE project_id = :p AND tool = :t ORDER BY recorded_at, id"),
            {"p": w.pid, "t": tool}).mappings()]


def _events(w, kind):
    with w.eng.begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT entity_id, data FROM events WHERE project_id = :p AND event_type = :e "
            "ORDER BY created_at"), {"p": w.pid, "e": kind}).mappings()]


def _gateway_verdict(w, tool, args=None, role="forge"):
    with w.eng.begin() as c:
        return mcp_gateway.tool_policy_verdict(_server(w), tool, arguments=args or {},
                                               project_id=w.pid, run_id="run_815",
                                               role_key=role, connection=c)


class FakeRegistry:
    def __init__(self):
        self.ran: list = []

    def get(self, name):
        if name not in ("list_things", "change_things"):
            raise RuntimeError(f"unknown tool {name}")
        return SimpleNamespace(kind="write" if name == "change_things" else "read")

    def invoke(self, name, ctx, *, project_id, actor, args, operator_confirmed=False):
        self.ran.append(name)
        return {"ok": True, "tool": name}


def _kernel(w, name, args=None, role="forge"):
    from boss_engine.kernel.state import RunState
    from boss_engine.kernel.toolset import RegistryToolSet
    from boss_engine.kernel.types import ToolCall

    reg = FakeRegistry()
    ts = RegistryToolSet(reg, SimpleNamespace(engine=w.eng), project_id=w.pid,
                         actor=f"agent:{role}", allow_writes=True)
    state = RunState(run_id="run_815", project_id=w.pid, lineage_id="l", system=[], messages=[])
    result = ts.invoke(ToolCall(id="c1", name=name, arguments=args or {}), state=state)
    return result, reg, ts


# ── the three plug points agree ──────────────────────────────────────────────────────────────

def test_no_rule_changes_nothing(world):
    w = world
    assert _gateway_verdict(w, "echo").decision == "allow"
    assert _gateway_verdict(w, "note").decision == "ask"
    assert _gateway_verdict(w, "write").decision == "ask"
    assert _call(w, "echo", {"text": "hi"})["output"]
    rows = _audits(w, "echo")
    assert rows[-1]["matched_policy_ids"] == ["base:allow"] and rows[-1]["screen"] is not None
    assert "policy" not in (rows[-1]["screen"] or {})
    result, reg, _ = _kernel(w, "list_things")
    assert result.status == "ok" and reg.ran == ["list_things"]


def test_a_deny_rule_is_the_same_answer_at_all_three_doors(world):
    from boss_engine.services.mcp_registry import McpToolDenied, McpToolOff

    w = world
    rule = _rule(w, effect="deny", selector={"server": w.name, "tool": "echo"},
                 conditions=[{"path": "$.text", "op": "prefix", "value": "rm "}])
    # gateway
    v = _gateway_verdict(w, "echo", {"text": "rm -rf"})
    assert v.decision == "deny" and v.rule_id == rule["id"] and v.reason == "policy"
    assert _gateway_verdict(w, "echo", {"text": "hello"}).decision == "allow"
    # receipt door
    with pytest.raises(McpToolDenied) as refused:
        _call(w, "echo", {"text": "rm -rf"})
    assert isinstance(refused.value, McpToolOff) and rule["id"] in str(refused.value)
    row = _audits(w, "echo")[-1]
    assert row["decision"] == "deny" and row["outcome"] == "refused"
    assert row["matched_policy_ids"] == [rule["id"]]
    # Post-819 who-semantics: the role rides on `agent`; `principal` is the acting identity.
    assert row["agent"] == "forge"
    assert row["screen"]["policy"]["rule_id"] == rule["id"]
    assert "rm " in row["screen"]["policy"]["explanation"]
    assert w.fake.calls == []
    _call(w, "echo", {"text": "hello"})
    assert w.fake.calls == [("echo", {"text": "hello"})]
    # kernel (BOSS's own tools are server "boss")
    _rule(w, effect="deny", selector={"server": "boss", "tool": "list_*"})
    result, reg, _ = _kernel(w, "list_things")
    assert result.status == "refused" and reg.ran == []
    assert result.content["error"] == "denied_by_policy"
    with w.eng.begin() as c:
        hits = c.execute(text("SELECT hit_count FROM tool_policies_v2 WHERE id = :i"),
                         {"i": rule["id"]}).scalar()
    assert hits == 1  # counted once, at the receipt door; the gateway read did not count


def test_an_allow_rule_lifts_a_tier_one_ask_but_never_a_tier_two(world):
    w = world
    _rule(w, effect="allow", selector={"server": "*", "tool": "*"}, scope="agent:forge")
    assert _gateway_verdict(w, "note").decision == "allow"
    assert _gateway_verdict(w, "note", role="scout").decision == "ask"
    v = _gateway_verdict(w, "write")
    assert v.decision == "ask" and "guard:tier_floor" in v.matched_policy_ids
    _call(w, "note", {"path": "a"})  # no approval needed now
    assert ("note", {"path": "a"}) in w.fake.calls
    from boss_engine.services.mcp_registry import McpServerUnreviewed

    with pytest.raises(McpServerUnreviewed):
        _call(w, "write", {})
    assert _audits(w, "write")[-1]["matched_policy_ids"][-1] == "guard:tier_floor"


def test_an_ask_rule_holds_the_kernel_call_for_a_person(world):
    w = world
    _rule(w, effect="ask", selector={"server": "boss", "tool": "list_things"})
    result, reg, ts = _kernel(w, "list_things")
    assert reg.ran == [] and result.content["status"] == "confirmation_required"
    assert ts.pending and ts.pending[0]["tool"] == "list_things"
    # another agent's rule does not reach this one
    _rule(w, effect="deny", scope="agent:scout", selector={"server": "boss", "tool": "*"})
    result, reg, _ = _kernel(w, "change_things", role="forge")
    assert result.status == "ok" and reg.ran == ["change_things"]


def test_a_rate_limit_counts_calls_that_ran(world):
    from boss_engine.services.mcp_registry import McpToolDenied

    w = world
    rl = _rule(w, effect="rate_limit", selector={"tool": "echo"},
               rate_limit={"max_calls": 2, "window_seconds": 3600})
    _call(w, "echo", {"text": "1"})
    _call(w, "echo", {"text": "2"})
    assert _gateway_verdict(w, "echo").decision == "deny"
    with pytest.raises(McpToolDenied):
        _call(w, "echo", {"text": "3"})
    assert _audits(w, "echo")[-1]["matched_policy_ids"] == [rl["id"]]


def test_the_gateway_honours_rules_end_to_end(world):
    """Through the live gateway: deny -> typed error, no card; allow on an Ask tool -> runs."""
    from boss_engine.services.mcp_client import MCPClient, forget_sessions

    w = world
    _rule(w, effect="deny", selector={"tool": "echo"})
    _rule(w, effect="allow", scope="agent:forge", selector={"tool": "note"})
    gateway = mcp_gateway.McpGateway(db=lambda: w.eng, settings=w.settings, poll_interval=0.05,
                                      keepalive=0.1, ask_timeout=3)
    try:
        token, base = gateway.issue(w.pid, {"fake": {"server_id": w.sid, "granted_tools": []}},
                                    role_key="forge", run_id=f"crun_{uuid.uuid4().hex[:6]}")
        forget_sessions()
        client = MCPClient(w.settings, timeout=20)
        from boss_engine.services.mcp_client import MCPToolError

        with pytest.raises(MCPToolError) as denied:
            client.call_tool(f"{base}/fake", "echo", {"text": "x"})
        assert "denied_by_policy" in str(denied.value) and "matched first" in str(denied.value)
        ran = client.call_tool(f"{base}/fake", "note", {"path": "p"})
        assert "done" in json.dumps(ran)
        with w.eng.begin() as c:
            cards = c.execute(text("SELECT count(*) FROM decisions WHERE project_id = :p AND "
                                   "recommendation->>'action' = 'gateway_tool_call'"),
                              {"p": w.pid}).scalar()
        assert cards == 0
    finally:
        gateway.stop()


# ── simulate + the API ───────────────────────────────────────────────────────────────────────

def test_simulate_decides_as_the_call_would_and_records_nothing(world):
    w = world
    r = _rule(w, effect="deny", selector={"tool": "echo"},
              conditions=[{"path": "$.text", "op": "regex", "value": "^drop"}])
    with w.eng.begin() as c:
        out = tpe.simulate(c, w.pid, {"server": w.name, "tool": "echo",
                                      "arguments": {"text": "drop table"}})
        quiet = tpe.simulate(c, w.pid, {"server": w.sid, "tool": "echo",
                                        "arguments": {"text": "select"}})
        boss = tpe.simulate(c, w.pid, {"server": "boss", "tool": "anything"})
        with pytest.raises(tpe.ToolPolicyError):
            tpe.simulate(c, w.pid, {"server": "nope", "tool": "x"})
        hits = c.execute(text("SELECT hit_count FROM tool_policies_v2 WHERE id = :i"),
                         {"i": r["id"]}).scalar()
    assert out["decision"] == "deny" and out["matched_rules"][0]["id"] == r["id"]
    assert quiet["decision"] == "allow" and quiet["matched_rules"] == []
    assert boss["decision"] == "allow" and out["context"]["tier"] == 1
    assert hits == 0


def test_the_routes(world):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apps.api.app.routers.tool_policies import build_tool_policies_router

    w = world
    app = FastAPI()
    app.include_router(build_tool_policies_router(
        engine=w.eng, require_reader=lambda c, p: None,
        require_write_actor=lambda c, p, a, perm: "ceo"))
    api = TestClient(app)
    made = api.post("/v1/tool-policies", json={
        "project_id": w.pid, "effect": "deny", "priority": 5,
        "selector": {"server": "*", "tool": "echo"}})
    assert made.status_code == 201, made.text
    rid = made.json()["policy"]["id"]
    bad = api.post("/v1/tool-policies", json={"project_id": w.pid, "effect": "maybe"})
    assert bad.status_code == 422
    org_allow = api.post("/v1/tool-policies", json={"project_id": w.pid, "effect": "allow",
                                                     "scope": "org"})
    assert org_allow.status_code == 422
    sim = api.post("/v1/tool-policies/simulate", json={
        "project_id": w.pid, "server": w.name, "tool": "echo"}).json()
    assert sim["decision"] == "deny" and sim["matched_policy_ids"] == [rid]
    off = api.patch(f"/v1/tool-policies/{rid}", json={"project_id": w.pid, "status": "disabled"})
    assert off.status_code == 200 and off.json()["policy"]["status"] == "disabled"
    sim = api.post("/v1/tool-policies/simulate", json={
        "project_id": w.pid, "server": w.name, "tool": "echo"}).json()
    assert sim["decision"] == "allow"
    listed = api.get("/v1/tool-policies", params={"project_id": w.pid}).json()
    assert [p["id"] for p in listed["policies"]] == [rid] and "never" in listed["expiry_choices"]
    gone = api.delete(f"/v1/tool-policies/{rid}", params={"project_id": w.pid})
    assert gone.json()["policy"]["status"] == "revoked"
    assert api.get("/v1/tool-policies", params={"project_id": w.pid}).json()["policies"] == []
    assert api.delete("/v1/tool-policies/nope", params={"project_id": w.pid}).status_code == 404
    assert _events(w, "mcp.tool_policy_revoked")[0]["entity_id"] == rid


# ── ALWAYS ALLOW (BOSSAPP-817) ───────────────────────────────────────────────────────────────

def _ask_card(w, tool, args):
    """The Ask card the gateway raises for a parked call (raise_decision, as call_upstream does)."""
    gateway = mcp_gateway.McpGateway(db=lambda: w.eng, settings=w.settings)
    g = SimpleNamespace(project_id=w.pid, run_key="run_815", run_id=None, role_key="forge")
    return gateway.raise_decision(g, kind=mcp_gateway.ACTION_TOOL_CALL, server=_server(w),
                                  tool=tool, arguments=args, reason="ask")


def _approve(w, decision_id, always_allow=None):
    from boss_engine.services.decisions import DecisionService

    with w.eng.begin() as c:
        return DecisionService(w.settings).transition(c, decision_id, action="approve",
                                                      actor="ceo", note=None,
                                                      always_allow=always_allow)


def test_always_allow_lifecycle(world):
    from boss_engine.services.mcp_registry import McpServerUnreviewed, record_reachability

    w = world
    with pytest.raises(McpServerUnreviewed):
        _call(w, "note", {"path": "/a"})  # tier 1, set to Ask: needs a person
    card = _ask_card(w, "note", {"path": "/a", "token": "s3cr3t"})
    with w.eng.begin() as c:
        rec = c.execute(text("SELECT recommendation FROM decisions WHERE id = :i"),
                        {"i": card}).scalar()
    offer = rec["always_allow_offer"]
    assert offer["eligible"] and offer["schema_sha256"] and "never" in offer["expiry_choices"]
    # a redacted value can never be pinned as a condition
    from boss_engine.services.decisions import InvalidDecisionTransitionError

    with pytest.raises(InvalidDecisionTransitionError):
        _approve(w, card, {"expiry": "7d", "match_args": ["token"]})
    with w.eng.begin() as c:
        assert c.execute(text("SELECT status FROM decisions WHERE id = :i"),
                         {"i": card}).scalar() == "pending"
    _approve(w, card, {"expiry": "7d", "match_args": ["path"]})
    with w.eng.begin() as c:
        rules = tpe.list_rules(c, w.pid, source="decision")
        rid = c.execute(text("SELECT recommendation->>'always_allow_rule_id' FROM decisions "
                             "WHERE id = :i"), {"i": card}).scalar()
    assert len(rules) == 1 and rules[0]["id"] == rid
    r = rules[0]
    assert r["scope"] == "agent:forge" and r["effect"] == "allow"
    assert r["selector"] == {"server": w.sid, "tool": "note"}
    assert r["conditions"] == [{"path": "$.path", "op": "equals", "value": "/a"}]
    assert r["schema_sha256"] == offer["schema_sha256"] and r["expires_at"] is not None
    assert _events(w, "mcp.always_allow_created")[0]["entity_id"] == rid
    # use: the same call runs with no approval and counts a hit; another path still asks
    _call(w, "note", {"path": "/a"})
    with pytest.raises(McpServerUnreviewed):
        _call(w, "note", {"path": "/b"})
    with pytest.raises(McpServerUnreviewed):
        _call(w, "note", {"path": "/a"}, role="scout")
    with w.eng.begin() as c:
        assert tpe.get_rule(c, w.pid, rid)["hit_count"] == 1
    assert len(_events(w, "mcp.always_allow_used")) == 1
    # 818: the tool's schema changes -> the rule is revoked, audited
    changed = [dict(t) for t in FakeMcp.TOOLS]
    changed[1] = {**changed[1], "inputSchema": {"type": "object", "properties": {
        "path": {"type": "string"}, "mode": {"type": "string"}}}}
    with w.eng.begin() as c:
        record_reachability(c, w.pid, w.sid, reachable=True, tools=changed)
        after = tpe.get_rule(c, w.pid, rid)
    assert after["status"] == "revoked" and after["revoked_reason"] == "schema_changed"
    revoked = _events(w, "mcp.always_allow_revoked")
    assert revoked and revoked[0]["entity_id"] == rid
    with pytest.raises(McpServerUnreviewed):
        _call(w, "note", {"path": "/a"})


def test_always_allow_revoked_by_hand_and_refused_where_not_offered(world):
    from boss_engine.services.decisions import InvalidDecisionTransitionError
    from boss_engine.services.mcp_registry import McpServerUnreviewed
    from boss_engine.services.tool_policies import list_always_allow, revoke_always_allow

    w = world
    card = _ask_card(w, "note", {"path": "/x"})
    _approve(w, card, {"expiry": "never"})
    with w.eng.begin() as c:
        rows = list_always_allow(c, project_id=w.pid)
    assert len(rows) == 1 and rows[0]["tool"] == "note" and rows[0]["server_id"] == w.sid
    _call(w, "note", {"path": "/anything"})  # no args condition: any arguments
    with w.eng.begin() as c:
        revoke_always_allow(c, project_id=w.pid, policy_id=rows[0]["id"], actor="ceo")
        assert list_always_allow(c, project_id=w.pid) == []
    with pytest.raises(McpServerUnreviewed):
        _call(w, "note", {"path": "/anything"})
    # tier 2: never offered, and refused if asked for anyway (the approval refused with it)
    card2 = _ask_card(w, "write", {})
    with w.eng.begin() as c:
        rec = c.execute(text("SELECT recommendation FROM decisions WHERE id = :i"),
                        {"i": card2}).scalar()
    assert rec["always_allow_offer"]["eligible"] is False
    with pytest.raises(InvalidDecisionTransitionError):
        _approve(w, card2, {"expiry": "never"})
    _approve(w, card2)  # approving the call alone still works
    # a card that is not about a tool call
    from boss_engine.services.core import create_blocking_decision

    with w.eng.begin() as c:
        other = create_blocking_decision(c, w.pid, None, "Proceed?",
                                         {"action": "gateway_ask_person", "run_id": "r"}, {},
                                         reason="gateway_ask_person", risk_tier=1,
                                         default_action="hold")
    with pytest.raises(InvalidDecisionTransitionError):
        _approve(w, other, {"expiry": "never"})


# ── the schema block ─────────────────────────────────────────────────────────────────────────

def test_the_schema_block_is_idempotent_and_matches_the_module(eng):
    psycopg = pytest.importorskip("psycopg")
    from boss_engine.db import split_statements

    sql = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    start = sql.index("-- BOSSAPP-815-817")
    end = sql.index("-- ============================ end TOOL POLICIES v2")
    block = sql[start:end]
    assert block.strip() == tpe.SCHEMA_SQL.strip()
    # Later tickets append their own blocks after this one; only BOSSAPP-tail blocks may follow
    # (the fresh-database clean-apply gate covers the whole file's integrity).
    tail = sql[end:].splitlines()[1:]
    assert all(l.strip() == "" or l.lstrip().startswith("--") or "BOSSAPP" in sql[end:] for l in tail[:1])
    url = PG_URL.replace("postgresql+psycopg://", "postgresql://")
    schema = f"t815_{uuid.uuid4().hex[:6]}"
    with psycopg.connect(url, autocommit=True) as c:
        c.execute(f'CREATE SCHEMA "{schema}"')
        try:
            c.execute(f'SET search_path TO "{schema}"')
            for _round in (1, 2):
                for statement in split_statements(block):
                    c.execute(statement)
            c.execute("INSERT INTO tool_policies_v2 (id, project_id, scope, effect) "
                      "VALUES ('a', 'p', 'agent:forge', 'deny')")
            for bad in ("INSERT INTO tool_policies_v2 (id, project_id, scope, effect) "
                        "VALUES ('b', 'p', 'project', 'maybe')",
                        "INSERT INTO tool_policies_v2 (id, project_id, scope, effect) "
                        "VALUES ('c', 'p', 'agent:', 'deny')",
                        "INSERT INTO tool_policies_v2 (id, scope, effect) "
                        "VALUES ('d', 'project', 'deny')"):
                with pytest.raises(psycopg.errors.CheckViolation):
                    c.execute(bad)
        finally:
            c.execute(f'DROP SCHEMA "{schema}" CASCADE')
