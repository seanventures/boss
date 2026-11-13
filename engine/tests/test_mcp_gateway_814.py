"""BOSSAPP-814: build agents can ask, wait for approval and report mid-run.

The coding harness's MCP traffic flows through BOSS's loopback MCP gateway (mcp_gateway.py)
when BOSS_MCP_GATEWAY=on. Pinned here:

  * OFF is byte-identical: code_task_mcp writes exactly the mcp.json and argv it always did;
  * ON points every server at the gateway (no upstream credential in the file), adds the run's
    `boss_run` tools, and stops disallowing Ask tools (Off/ungranted stay disallowed);
  * the gateway proxies a real (fake) MCP server transparently: tools/list and tools/call
    through it read the same as direct;
  * the policy seam answers allow / ask / deny from the registry's tier and trust data;
  * an ask call PARKS: a decisions row in the Approvals queue, the run's stage clock stopped,
    keepalives on the open stream; approving it (DecisionService, as the console does) runs the
    call and delivers the result; declining it returns a typed error; nobody answering withdraws
    the card and returns a typed timeout;
  * the five self-service tools round-trip (ask_person, request_approval, report_blocked,
    report_progress, propose_followup) and the reports land as run events;
  * the stage clock pauses: deadlines move out by the parked time, overlapping parks count once,
    and the governed implement budget reads it through its one seam.

The DB tests need BOSS_TEST_DATABASE_URL naming a disposable database (full schema).
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import code_task_mcp, mcp_gateway, stage_clock
from boss_engine.services import decision_actions as da

ROOT = Path(__file__).resolve().parents[2]
PG_URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")


# ── a fake upstream MCP server ───────────────────────────────────────────────────────────────

class FakeMcp:
    """A Streamable-HTTP MCP server with three tools; it remembers every call it ran."""

    TOOLS = [
        {"name": "echo", "description": "Echo the text back.",
         "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
        {"name": "write", "description": "Writes something (asks a person).",
         "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}},
        {"name": "wipe", "description": "Turned off.", "inputSchema": {"type": "object"}},
    ]

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
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
                    args = params.get("arguments") or {}
                    result = {"content": [{"type": "text", "text": json.dumps(
                        {"tool": params["name"], "args": args})}]}
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


# ── OFF: byte-identical; ON: the gateway's file ──────────────────────────────────────────────

def _held(url: str = "https://docs.example/mcp") -> list[dict]:
    return [
        {"id": "mcp_docs", "name": "Library docs", "url": url, "risk_tier": 1,
         "tools": [{"name": "read"}, {"name": "write"}, {"name": "wipe"}, {"name": "secret"}],
         "granted_tools": ["read", "write", "wipe"], "tool_policy": {"wipe": "off"},
         "provenance": {"trusted": True}},
        {"id": "mcp_new", "name": "unreviewed", "url": "https://new.example/mcp",
         "risk_tier": 1, "tools": [{"name": "look"}], "granted_tools": [],
         "provenance": {"trusted": False}},
    ]


@pytest.fixture()
def held(monkeypatch):
    from boss_engine.services import agents as agents_mod, mcp_registry

    servers = _held()
    monkeypatch.setattr(agents_mod, "held_mcp_servers", lambda c, p, r: servers)
    monkeypatch.setattr(mcp_registry, "needs_person",
                        lambda s: not (s.get("provenance") or {}).get("trusted"))
    policies = {"read": "allow", "write": "ask", "wipe": "off", "secret": "allow",
                "look": "allow"}
    monkeypatch.setattr(mcp_registry, "tool_policy", lambda s, t, *a: policies[t])
    monkeypatch.setattr(mcp_registry, "auth_headers",
                        lambda c, st, p, s: {"Authorization": "Bearer upstream-own"})
    return servers


@pytest.mark.parametrize("flag", [None, "off", "0", ""])
def test_mcp_gateway_off_is_byte_identical(held, monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv(mcp_gateway.FLAG, raising=False)
    else:
        monkeypatch.setenv(mcp_gateway.FLAG, flag)
    harness = code_task_mcp.prepare(None, None, "p", "forge")
    try:
        written = Path(harness.path).read_bytes()
        # Exactly what the pre-gateway code wrote: json.dump of the direct servers, their own
        # sign-in in the file, every non-Allow and ungranted tool disallowed, unreviewed left out.
        expected = json.dumps({"mcpServers": {"Library_docs": {
            "type": "http", "url": "https://docs.example/mcp",
            "headers": {"Authorization": "Bearer upstream-own"}}}}).encode()
        assert written == expected
        assert harness.argv() == [
            "--strict-mcp-config", "--mcp-config", harness.path, "--disallowedTools",
            "mcp__Library_docs__secret,mcp__Library_docs__wipe,mcp__Library_docs__write"]
        assert harness.gateway_token == "" and harness.env() == {}
        # Not run as an agent: no file at all, as before.
        assert code_task_mcp.prepare(None, None, "p", "") is None
    finally:
        harness.cleanup()


def test_gateway_on_points_every_harness_at_the_gateway(held, monkeypatch):
    monkeypatch.setenv(mcp_gateway.FLAG, "on")
    gateway = mcp_gateway.McpGateway()
    mcp_gateway.set_gateway(gateway)
    try:
        harness = code_task_mcp.prepare(None, None, "p", "forge", run_id="crun_x")
        written = json.loads(Path(harness.path).read_text())["mcpServers"]
        token = harness.gateway_token
        assert token.startswith(mcp_gateway.TOKEN_PREFIX)
        base = f"http://127.0.0.1:{gateway.port}/v1/mcp-gateway/{token}"
        # Every server is the gateway; the unreviewed one is no longer left out (it parks);
        # the run's own tools are there; no upstream credential is in the file.
        assert written == {"Library_docs": {"type": "http", "url": f"{base}/Library_docs"},
                           "unreviewed": {"type": "http", "url": f"{base}/unreviewed"},
                           "boss_run": {"type": "http", "url": f"{base}/boss_run"}}
        assert "upstream-own" not in Path(harness.path).read_text()
        # Ask tools route through the gateway's ask flow; Off and ungranted stay refused.
        assert harness.argv()[-1] == "mcp__Library_docs__secret,mcp__Library_docs__wipe"
        assert int(harness.env()["MCP_TOOL_TIMEOUT"]) > mcp_gateway.ask_timeout_seconds() * 1000
        grant = gateway.grant_for(token)
        assert grant.run_id == "crun_x" and grant.project_id == "p"
        assert grant.servers["Library_docs"] == {"server_id": "mcp_docs",
                                                 "granted_tools": ["read", "wipe", "write"]}
        # A session not run as an agent still gets boss_run.
        solo = code_task_mcp.prepare(None, None, "p", None)
        assert set(json.loads(Path(solo.path).read_text())["mcpServers"]) == {"boss_run"}
        solo.cleanup()
        harness.cleanup()
        assert gateway.authorise(token)[1] == "token_revoked"
    finally:
        gateway.stop()
        mcp_gateway.set_gateway(None)


def test_a_late_token_is_bound_by_the_first_session(monkeypatch):
    from boss_engine.services.llm_telemetry import set_llm_context

    gateway = mcp_gateway.McpGateway()
    mcp_gateway.set_gateway(gateway)
    try:
        token, _ = gateway.issue("p", {})
        harness = code_task_mcp.HarnessMcp(path="", servers=[], gateway_token=token)
        set_llm_context(run_id="crun_first")
        harness.argv()
        set_llm_context(run_id="crun_other")
        harness.argv()
        assert gateway.grant_for(token).run_id == "crun_first"
    finally:
        set_llm_context(run_id=None)
        gateway.stop()
        mcp_gateway.set_gateway(None)


# ── the policy seam ──────────────────────────────────────────────────────────────────────────

def _server(**over):
    base = {"id": "mcp_1", "name": "s", "url": "http://x/mcp", "risk_tier": 1,
            "provenance": {"trusted": True}, "tool_policy": {}, "tool_risk_overrides": {},
            "tools": []}
    base.update(over)
    return base


def test_policy_verdicts_allow_ask_deny():
    v = mcp_gateway.tool_policy_verdict
    assert v(_server(), "read") == mcp_gateway.Verdict("allow", "allow")
    assert v(_server(tool_policy={"read": "ask"}), "read") == mcp_gateway.Verdict("ask", "ask")
    # Tier 2 floors at Ask, whatever the operator chose.
    assert v(_server(risk_tier=2, tool_policy={"w": "allow"}), "w").decision == "ask"
    assert v(_server(risk_tier=3), "nuke") == mcp_gateway.Verdict("deny", "off")
    assert v(_server(tool_policy={"read": "off"}), "read") == mcp_gateway.Verdict("deny", "off")
    assert v(_server(), "read", granted_tools=["other"]) == mcp_gateway.Verdict(
        "deny", "not_granted")
    assert v(_server(provenance={"trusted": False}), "read") == mcp_gateway.Verdict(
        "ask", "unreviewed")
    # Off beats unreviewed: nothing to ask about a tool that never runs.
    assert v(_server(provenance={"trusted": False}, tool_policy={"read": "off"}),
             "read").decision == "deny"


def test_the_gateway_cards_say_who_acts_on_them():
    for action in da.PARKED_CALL_ACTIONS:
        assert action in da.RECORDED_ON_APPROVAL
        card = {"status": "pending", "recommendation": {"action": action}}
        approve = next(a for a in da.allowed_actions(card) if a["action"] == "approve")
        assert "starts nothing" in approve["effect"] and "waiting" in approve["effect"]
        assert da.allowed_actions({**card, "status": "approved"}) == []


# ── the stage clock ──────────────────────────────────────────────────────────────────────────

def test_the_stage_clock_pauses_while_parked():
    run = f"crun_{uuid.uuid4().hex[:8]}"
    since = time.time()
    deadline = since + 100
    assert stage_clock.extended(run, deadline, since=since) == deadline
    with stage_clock.parked(run):
        assert stage_clock.is_parked(run)
        with stage_clock.parked(run):            # a parallel ask: one pause, not two
            time.sleep(0.2)
        time.sleep(0.1)
    assert not stage_clock.is_parked(run)
    paused = stage_clock.paused_between(run, since)
    assert 0.28 < paused < 0.6
    assert stage_clock.extended(run, deadline, since=since) == pytest.approx(deadline + paused)
    # A pause before the deadline was set does not move it.
    assert stage_clock.extended(run, deadline, since=time.time()) == deadline
    stage_clock.forget(run)
    assert stage_clock.paused_between(run, since) == 0.0


def test_a_session_deadline_moves_out_by_the_parked_time(monkeypatch):
    from boss_engine.services import code_agent
    from boss_engine.services.llm_telemetry import set_llm_context

    run = f"crun_{uuid.uuid4().hex[:8]}"
    deadline = time.time() + 50
    set_llm_context(run_id=run, stage_deadline=deadline)
    try:
        assert code_agent._stage_deadline() == deadline       # first read anchors the stage
        with stage_clock.parked(run):
            time.sleep(0.2)
        assert code_agent._stage_deadline() >= deadline + 0.19
    finally:
        set_llm_context(run_id=None, stage_deadline=None)
        stage_clock.forget(run)


def test_the_governed_implement_budget_reads_the_pause_through_one_seam():
    source = (ROOT / "engine/boss_engine/services/governed_code_task_service.py").read_text()
    start = source.index("def _impl_over_budget")
    body = source[start:source.index("def _within_budget", start)]
    assert "stage_clock.extended(" in body and "since=_impl_deadline - _impl_budget_s" in body
    assert source.count("stage_clock") == 2           # the import and the call: one seam


def test_a_parked_session_is_not_killed_as_stalled(monkeypatch):
    """code_agent._drain: silence while parked is not a stall, and the parked time is added to
    the session's timeout."""
    import subprocess
    import sys

    from boss_engine.services import code_agent
    from boss_engine.services.llm_telemetry import set_llm_context

    run = f"crun_{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv(mcp_gateway.FLAG, "on")
    monkeypatch.setattr(code_agent, "_idle_timeout_seconds", lambda: 0.5)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2.5); print('ok')"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    set_llm_context(run_id=run)
    hold = threading.Event()

    def park():
        with stage_clock.parked(run):
            hold.wait(5)

    t = threading.Thread(target=park)
    t.start()
    try:
        # Without the pause the 0.5 s idle limit (and the 1.5 s timeout) would end it.
        orig_wait = child.wait
        monkeypatch.setattr(child, "wait", lambda timeout=None: orig_wait(
            timeout=min(timeout or 0.2, 0.2)))
        out, _ = code_agent._drain(child, timeout=1.5)
        assert "ok" in out
    finally:
        hold.set()
        t.join()
        set_llm_context(run_id=None)
        stage_clock.forget(run)


# ── the gateway against a real database ──────────────────────────────────────────────────────

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
    from boss_engine.services.mcp_registry import register_mcp_server

    pid = f"p_814_{uuid.uuid4().hex[:8]}"
    fake = FakeMcp()
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_814', 'o') "
                       "ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_814', 'org_814', "
                       "'w') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                       "(:p, 'ws_814', :p, :p)"), {"p": pid})
        c.execute(text(
            "INSERT INTO project_members (id, project_id, member_id, display_name, member_type, "
            "role_keys, permissions) VALUES (:i, :p, 'ceo', 'ceo', 'human', '[\"ceo\"]'::jsonb, "
            "'[\"decision_resolve\"]'::jsonb)"), {"i": f"pm_{pid}", "p": pid})
        server = register_mcp_server(
            c, pid, f"fake-{pid}", fake.url, risk_tier=1, provenance={"trusted": True},
            tool_policy={"write": "ask", "wipe": "off"}, actor="ceo")
        c.execute(text("UPDATE mcp_servers SET tools = CAST(:t AS JSONB) WHERE id = :i"),
                  {"t": json.dumps(FakeMcp.TOOLS), "i": server["id"]})
    settings = get_settings()
    gateway = mcp_gateway.McpGateway(db=lambda: eng, settings=settings, poll_interval=0.05,
                                     keepalive=0.1, ask_timeout=10)
    run_id = f"crun_814_{uuid.uuid4().hex[:6]}"
    token, base = gateway.issue(pid, {"fake": {"server_id": server["id"], "granted_tools": []}},
                                role_key="forge", run_id=run_id)
    yield {"eng": eng, "pid": pid, "fake": fake, "gateway": gateway, "token": token,
           "base": base, "run": run_id, "server": server, "settings": settings}
    gateway.stop()
    fake.stop()
    stage_clock.forget(run_id)


def _client(world):
    from boss_engine.services.mcp_client import MCPClient, forget_sessions

    forget_sessions()
    return MCPClient(world["settings"], timeout=20)


def _pending(world, action):
    with world["eng"].begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT id, question, recommendation, status FROM decisions WHERE project_id = :p "
            "AND recommendation->>'action' = :a ORDER BY created_at"),
            {"p": world["pid"], "a": action}).mappings()]


def _wait_for_card(world, action, n=1, seconds=10.0):
    end = time.time() + seconds
    while time.time() < end:
        rows = [r for r in _pending(world, action) if r["status"] == "pending"]
        if len(rows) >= n:
            return rows[-1]
        time.sleep(0.05)
    raise AssertionError(f"no pending {action} decision")


def _answer(world, decision_id, action, note=None):
    from boss_engine.config import get_settings
    from boss_engine.services.decisions import DecisionService

    with world["eng"].begin() as c:
        DecisionService(get_settings()).transition(c, decision_id, action=action, actor="ceo",
                                                   note=note)


def _in_thread(fn):
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, box


def _audit(world, tool):
    with world["eng"].begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT decision, outcome, decision_id, run_id FROM tool_call_audit "
            "WHERE project_id = :p AND tool = :t ORDER BY recorded_at"),
            {"p": world["pid"], "t": tool}).mappings()]


def test_the_gateway_proxies_a_server_transparently(world):
    client = _client(world)
    direct = client.list_tools(world["fake"].url)
    through = client.list_tools(f"{world['base']}/fake")
    assert [t["name"] for t in through] == [t["name"] for t in direct] == ["echo", "write", "wipe"]
    assert through == direct
    said = client.call_tool(f"{world['base']}/fake", "echo", {"text": "hi"})
    assert said == {"tool": "echo", "args": {"text": "hi"}}
    assert world["fake"].calls == [("echo", {"text": "hi"})]
    # The receipt and the audit row of an allowed call, bound to the run.
    [row] = _audit(world, "echo")
    assert row["decision"] == "allow" and row["outcome"] == "succeeded"
    assert row["run_id"] == world["run"]
    # Anything else on the URL is refused before a byte goes upstream.
    bad = httpx.post(world["base"].replace(world["token"], world["token"][:-2] + "00") + "/fake",
                     json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert bad.status_code == 401


def test_an_ask_call_parks_until_approved_then_runs(world):
    client = _client(world)
    t, box = _in_thread(lambda: client.call_tool(f"{world['base']}/fake", "write",
                                                 {"path": "a.py"}))
    card = _wait_for_card(world, "gateway_tool_call")
    rec = card["recommendation"]
    assert rec["run_id"] == world["run"] and rec["tool"] == "write" and rec["args_sha256"]
    assert "write" in card["question"]
    assert stage_clock.is_parked(world["run"])            # the stage clock is stopped
    assert world["fake"].calls == []                       # nothing ran while parked
    time.sleep(0.3)
    _answer(world, card["id"], "approve", note="fine")
    t.join(15)
    assert box.get("value") == {"tool": "write", "args": {"path": "a.py"}}, box
    assert world["fake"].calls == [("write", {"path": "a.py"})]
    assert not stage_clock.is_parked(world["run"])
    assert stage_clock.paused_between(world["run"], time.time() - 60) >= 0.3
    [row] = _audit(world, "write")
    assert (row["decision"], row["outcome"], row["decision_id"]) == ("ask", "succeeded",
                                                                     card["id"])
    with world["eng"].begin() as c:
        kinds = [r[0] for r in c.execute(text(
            "SELECT event_type FROM events WHERE entity_id = :r ORDER BY created_at"),
            {"r": world["run"]})]
    assert "code_task.agent_waiting" in kinds and "code_task.agent_resumed" in kinds


def test_a_parked_call_streams_keepalives(world):
    body = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "write", "arguments": {"path": "k.py"},
                       "_meta": {"progressToken": "pt1"}}}
    lines: list[str] = []

    def stream():
        with httpx.stream("POST", f"{world['base']}/fake", json=body, timeout=20, headers={
                "accept": "application/json, text/event-stream"}) as resp:
            assert resp.headers["content-type"].startswith("text/event-stream")
            for line in resp.iter_lines():
                lines.append(line)
        return True

    t, box = _in_thread(stream)
    card = _wait_for_card(world, "gateway_tool_call")
    time.sleep(0.4)
    _answer(world, card["id"], "approve")
    t.join(15)
    assert box.get("value") is True, box
    assert ": keepalive" in lines
    events = [json.loads(line[5:]) for line in lines if line.startswith("data:")]
    assert any(e.get("method") == "notifications/progress"
               and e["params"]["progressToken"] == "pt1" for e in events)
    final = events[-1]
    assert final["id"] == 7 and final["result"]["isError"] is False


def test_a_declined_call_returns_a_typed_error(world):
    from boss_engine.services.mcp_client import MCPToolError

    client = _client(world)
    t, box = _in_thread(lambda: client.call_tool(f"{world['base']}/fake", "write",
                                                 {"path": "b.py"}))
    card = _wait_for_card(world, "gateway_tool_call")
    _answer(world, card["id"], "reject", note="not that file")
    t.join(15)
    assert isinstance(box.get("error"), MCPToolError), box
    assert "[boss.denied_by_person]" in str(box["error"]) and "not that file" in str(box["error"])
    assert world["fake"].calls == []
    [row] = _audit(world, "write")
    assert (row["decision"], row["outcome"]) == ("ask", "refused")


def test_a_policy_denied_call_is_refused_and_audited(world):
    out = world["gateway"].call_upstream(world["gateway"].grant_for(world["token"]), "fake",
                                         "wipe", {})
    assert out["isError"] is True
    assert out["structuredContent"]["error"]["type"] == "boss.denied_by_policy"
    assert out["structuredContent"]["error"]["reason"] == "off"
    assert world["fake"].calls == []
    [row] = _audit(world, "wipe")
    assert (row["decision"], row["outcome"]) == ("deny", "refused")


def test_nobody_answering_withdraws_the_card_and_says_so(world):
    gateway = world["gateway"]
    gateway._ask_timeout = 0.4
    out = gateway.call_upstream(gateway.grant_for(world["token"]), "fake", "write",
                                {"path": "c.py"})
    assert out["structuredContent"]["error"]["type"] == "boss.approval_timed_out"
    [card] = _pending(world, "gateway_tool_call")
    assert card["status"] == "withdrawn"
    assert world["fake"].calls == []


def test_the_run_tools_round_trip(world):
    client = _client(world)
    url = f"{world['base']}/boss_run"
    names = [t["name"] for t in client.list_tools(url)]
    assert names == ["ask_person", "request_approval", "report_blocked", "report_progress",
                     "propose_followup"]
    assert client.call_tool(url, "report_progress", {"note": "tests green"})["recorded"]
    assert client.call_tool(url, "report_blocked", {"reason": "no DB creds"})["recorded"]
    assert client.call_tool(url, "propose_followup",
                            {"title": "Split the module", "body": "too big"})["recorded"]
    with world["eng"].begin() as c:
        events = {r[0]: r[1] for r in c.execute(text(
            "SELECT event_type, data FROM events WHERE entity_type = 'code_task_run' "
            "AND entity_id = :r"), {"r": world["run"]})}
    assert events["code_task.agent_progress"]["note"] == "tests green"
    assert events["code_task.agent_blocked"]["reason"] == "no DB creds"
    assert events["code_task.followup_proposed"]["body"] == "too big"

    # ask_person parks on a card and returns the person's words.
    t, box = _in_thread(lambda: client.call_tool(url, "ask_person", {
        "question": "Which database?", "options": ["postgres", "sqlite"]}))
    card = _wait_for_card(world, "gateway_ask_person")
    assert "Which database?" in card["question"] and "postgres" in card["question"]
    _answer(world, card["id"], "approve", note="postgres")
    t.join(15)
    assert box["value"]["answer"] == "postgres" and box["value"]["approved"] is True

    # request_approval: declined is a typed error.
    from boss_engine.services.mcp_client import MCPToolError

    t, box = _in_thread(lambda: client.call_tool(url, "request_approval", {
        "action": "drop the legacy table", "args": {"table": "old"}}))
    card = _wait_for_card(world, "gateway_request_approval")
    assert card["recommendation"]["args_sha256"]
    _answer(world, card["id"], "reject")
    t.join(15)
    assert isinstance(box.get("error"), MCPToolError)
    assert "[boss.denied_by_person]" in str(box["error"])
    # And approved is a plain answer.
    t, box = _in_thread(lambda: client.call_tool(url, "request_approval", {
        "action": "rename the module"}))
    card = _wait_for_card(world, "gateway_request_approval")
    _answer(world, card["id"], "approve")
    t.join(15)
    assert box["value"]["approved"] is True


def test_a_revoked_run_releases_its_parked_call(world):
    gateway = world["gateway"]
    grant = gateway.grant_for(world["token"])
    t, box = _in_thread(lambda: gateway.call_run_tool(grant, "ask_person",
                                                      {"question": "Still there?"}))
    card = _wait_for_card(world, "gateway_ask_person")
    gateway.revoke(world["token"])
    t.join(10)
    assert box["value"]["structuredContent"]["error"]["type"] == "boss.run_ended"
    [row] = [r for r in _pending(world, "gateway_ask_person") if r["id"] == card["id"]]
    assert row["status"] == "withdrawn"
