"""AP-P5 acceptance on a real database (docs/design/AGENT_PLATFORM.md §5 Phase 5; BOSSAPP-1056).

(a) A 2026-07-28 MCP client discovers, calls a read tool, starts a code task as an MCP task,
    polls it to completion, and answers an `input_required` approval that BOSS validates with
    step-up (a key is refused and pointed at the console; a stale session is refused; a person's
    freshly stepped-up session is accepted). Plus: a deployed agent called synchronously answers
    `input_required` (MRTR) and completes on the retry, its run joining the caller's trace.
(b) An external A2A client verifies a BOSS agent card's signature against the agent's JWKS, sends
    a message, receives input-required, resumes, and gets a push notification on completion.
    D11: A2A is off by default, refused for an agent with write tools, and an unsigned agent is
    never served.

The official MCP and A2A SDKs are not installed in this environment, so the clients here are
minimal spec-conformant test clients speaking the wire format over the real HTTP transports
(MCPServer.asgi_app, the API's /a2a routes).
"""
from __future__ import annotations

import json
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.tests.test_ap_p3_deployments_and_bots import (OBJ, Parts, _deploy, _drain,  # noqa: F401
                                                            parts)

MODERN = "2026-07-28"
TASKS = "io.modelcontextprotocol/tasks"


# ── minimal clients ────────────────────────────────────────────────────────────────────────────

class ModernMCPClient:
    """A stateless 2026-07-28 client: `_meta` on every request (version, client info,
    capabilities: the tasks extension and form + URL elicitation), MCP-Protocol-Version and
    Mcp-Method headers, a fresh id per request, no initialize, no session."""

    def __init__(self, http: TestClient, bearer: str, *, traceparent: str = "",
                 tasks: bool = True):
        self.http, self.bearer, self.traceparent, self.n = http, bearer, traceparent, 0
        self.tasks = tasks

    def rpc(self, method: str, params: dict | None = None, *, bearer: str = "") -> dict:
        self.n += 1
        meta = {"io.modelcontextprotocol/protocolVersion": MODERN,
                "io.modelcontextprotocol/clientInfo": {"name": "p5-test", "version": "1"},
                "io.modelcontextprotocol/clientCapabilities": {
                    **({"extensions": {TASKS: {}}} if self.tasks else {}),
                    "elicitation": {"form": {}, "url": {}}}}
        if self.traceparent:
            meta["traceparent"] = self.traceparent
        body = {"jsonrpc": "2.0", "id": f"r{self.n}", "method": method,
                "params": {**(params or {}), "_meta": meta}}
        resp = self.http.post("/mcp", json=body, headers={
            "Authorization": f"Bearer {bearer or self.bearer}", "MCP-Protocol-Version": MODERN,
            "Mcp-Method": method})
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["id"] == body["id"]
        return out


def _key(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:6]}"


def _agent(engine, project: str, key: str, *, spec_tools: dict | None = None) -> None:
    """A real agent (roles row, identity, v1 published) from a spec document."""
    from boss_engine.agents import versions

    with engine.begin() as c:
        versions.create_from_spec(c, project, {
            "apiVersion": "boss.agent/v1", "kind": "Agent",
            "metadata": {"key": key, "name": "Triage bot"},
            "spec": {"mission": "Triage support issues and close duplicates.",
                     "prompts": {"system": "I am v1."}, "limits": {"max_steps": 6},
                     **({"tools": spec_tools} if spec_tools else {})}},
            actor="ceo", source="operator")


@pytest.fixture
def world(engine):
    from apps.api.tests.identity_fixtures import IdentityWorld

    w = IdentityWorld.create(engine)
    w.claimed()
    w.owner = w.person("Olive Owner")
    w.bind(w.owner, "owner")
    w.person("Second Person")
    return w


def _sa(world, engine, name: str, permissions: tuple):
    from boss_engine.identity.principal import AuthContext, AuthMethod, CredentialRef, PrincipalKind

    sa = world.principal(name, kind=PrincipalKind.SERVICE_ACCOUNT)
    world.bind(sa, world.custom_role(name.lower().replace(" ", "_"), permissions),
               project=world.project_a)
    k = world.key_ctx(sa, permissions)
    with engine.begin() as db:
        db.execute(text("UPDATE identity_api_credentials SET kind = 'sak' WHERE id = :k"),
                   {"k": k.credential.id})
    return AuthContext(sa, AuthMethod.API_KEY, credential=CredentialRef(
        id=k.credential.id, kind="sak", principal_id=sa.id, scopes=k.credential.scopes))


@pytest.fixture
def mcp(engine, world, monkeypatch):
    """BOSS's MCP server over its real HTTP transport, native (it holds the engine), under
    enforce; each bearer in `tokens` is the verified identity the transport would produce."""
    from boss_engine.config import get_settings
    from boss_engine.context import EngineContext
    from boss_engine.mcp import server as mcp_server

    settings = get_settings()
    monkeypatch.setattr(settings, "authz_mode", "enforce")
    monkeypatch.setattr(settings, "authz_router_modes", "")
    tokens: dict = {}
    monkeypatch.setattr(mcp_server, "_is_boss_credential", lambda tok: tok in tokens)
    monkeypatch.setattr(mcp_server, "authenticate_transport",
                        lambda headers, **kw: tokens.get(
                            str(dict((k.lower(), v) for k, v in headers.items())
                                .get("authorization", "")).partition(" ")[2]))
    ctx = EngineContext(settings=settings, engine=engine)
    srv = mcp_server.MCPServer(ctx, project_id=world.project_a, actor="mcp",
                               http_token="shared-legacy-token")
    http = TestClient(srv.asgi_app(), raise_server_exceptions=True)
    return http, tokens, srv


def _worker(engine, project: str):
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            _drain(engine, project, rounds=1)
            time.sleep(0.1)

    th = threading.Thread(target=loop, daemon=True)
    th.start()
    return stop, th


# ── (a) MCP 2026-07-28 ─────────────────────────────────────────────────────────────────────────

def test_a_modern_mcp_discovers_reads_runs_a_code_task_as_a_task_and_answers_with_step_up(
        engine, world, mcp, monkeypatch):
    from boss_engine.services import code_task_service
    from boss_engine.services.interrupts import open_interrupt, plan_interrupt
    from boss_engine.services.core import create_blocking_decision

    http, tokens, _ = mcp
    project = world.project_a
    perms = ("code_task:read", "code_task:create", "agent:invoke", "decision:read",
             "decision:approve", "project:read")
    tokens["test-pat"] = world.key_ctx(world.owner, perms)              # a bearer: cannot step up
    tokens["test-stale"] = world.ctx(world.owner, mfa=False)            # a session, no 2nd factor
    tokens["test-fresh"] = world.ctx(world.owner, mfa=True)             # stepped up just now
    client = ModernMCPClient(http, "test-pat")

    # DISCOVER: stateless, every revision named, the tasks extension advertised.
    found = client.rpc("server/discover")["result"]
    assert found["resultType"] == "complete"
    assert {"2026-07-28", "2025-11-25", "2024-11-05"} <= set(found["supportedVersions"])
    assert TASKS in found["capabilities"]["extensions"]
    assert found["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "boss-engine"
    bad = client.http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                                         "params": {"_meta": {
                                             "io.modelcontextprotocol/protocolVersion":
                                                 "1900-01-01"}}},
                           headers={"Authorization": "Bearer test-pat"}).json()
    assert bad["error"]["code"] == -32022 and "2026-07-28" in bad["error"]["data"]["supported"]

    # A READ TOOL, from a deterministic list.
    listed = client.rpc("tools/list")["result"]
    names = [tool["name"] for tool in listed["tools"]]
    assert names == sorted(names) and listed["cacheScope"] == "private" and listed["ttlMs"] > 0
    assert client.rpc("tools/list")["result"]["tools"] == listed["tools"], "same order twice"
    read = client.rpc("tools/call", {"name": "list_code_tasks", "arguments": {}})["result"]
    assert read["resultType"] == "complete" and read["isError"] is False

    # START A CODE TASK AS AN MCP TASK: a durable handle, not a blocking call.
    from boss_engine.services import repo_registry

    monkeypatch.setattr(code_task_service, "start_code_task",
                        _fake_start_code_task(engine, project))
    monkeypatch.setattr(repo_registry, "resolve_repository_target", lambda *a, **k: {
        "owner": "acme", "repo": "app", "full_name": "acme/app", "source": "binding",
        "binding_id": "rb_test"})
    started = client.rpc("tools/call", {"name": "create_code_task", "arguments": {
        "title": "Fix the login copy", "instructions": "Change 'Sign on' to 'Sign in'."}})
    created = started["result"]
    assert created["resultType"] == "task", created
    task_id = created["taskId"]
    assert created["status"] == "working" and created["pollIntervalMs"] > 0
    assert "result" not in created and "inputRequests" not in created
    run_id = _only_code_task(engine, project)
    assert client.rpc("tasks/get", {"taskId": task_id})["result"]["status"] == "working"

    # THE BUILD STOPS AT ITS PLAN GATE (an interrupt = a Decision), as the pipeline opens it.
    gate = plan_interrupt(run_id, {"plan": "edit login.tsx", "plan_ok": True})
    with engine.begin() as c:
        decision_id = open_interrupt(
            c, project_id=project, task_id=None, item=gate,
            question="Approve the technical plan for “Fix the login copy” before BOSS writes code?",
            recommendation={"action": "resume_code_task", "run_id": run_id,
                             "plan": "edit login.tsx"},
            reason="plan gate", risk_tier=1, create=create_blocking_decision)
        c.execute(text("UPDATE code_task_runs SET status = 'blocked', "
                       "phase = 'awaiting_plan_approval' WHERE id = :r"), {"r": run_id})
    polled = client.rpc("tasks/get", {"taskId": task_id})["result"]
    assert polled["status"] == "input_required", polled
    request = polled["inputRequests"][f"decision:{decision_id}"]
    # The PAT cannot approve (no step-up for a bearer), so it is shown the console, URL mode.
    assert request["method"] == "elicitation/create"
    assert request["params"]["mode"] == "url"
    assert request["params"]["url"].endswith(f"/decisions/{decision_id}")
    fresh_view = client.rpc("tasks/get", {"taskId": task_id}, bearer="test-fresh")["result"]
    assert fresh_view["inputRequests"][f"decision:{decision_id}"]["params"]["mode"] == "form"

    answer = {f"decision:{decision_id}": {"action": "accept",
                                          "content": {"verdict": "approve", "note": "looks right"}}}
    # BOSS VALIDATES THE ANSWER WITH STEP-UP: a key is refused, a stale session is refused...
    for bearer in ("test-pat", "test-stale"):
        refused = client.rpc("tasks/update", {"taskId": task_id, "inputResponses": answer},
                             bearer=bearer)
        assert refused["error"]["code"] == -32010, refused
        assert refused["error"]["data"]["refused"][decision_id] == "step_up_required"
        assert _decision(engine, decision_id)["status"] == "pending"
    # ...a forged answer to a decision that is not this task's is ignored...
    assert "error" not in client.rpc("tasks/update", {"taskId": task_id, "inputResponses": {
        "decision:dec_not_this_one": {"action": "accept", "content": {"verdict": "approve"}}}},
        bearer="test-fresh")
    # ...and the person's freshly stepped-up session is accepted, as the Decision API would.
    ok = client.rpc("tasks/update", {"taskId": task_id, "inputResponses": answer},
                    bearer="test-fresh")
    assert ok["result"] == {"resultType": "complete", "_meta": ok["result"]["_meta"]}, ok
    decided = _decision(engine, decision_id)
    assert decided["status"] in ("approved", "executed")
    assert decided["approved_by"] in (world.owner.id, "ceo") or decided["approved_by"]

    # THE BUILD GOES ON AND ENDS; the client polls it to completion.
    with engine.begin() as c:
        c.execute(text("UPDATE code_task_runs SET status = 'running', phase = 'coding' "
                       "WHERE id = :r"), {"r": run_id})
    assert client.rpc("tasks/get", {"taskId": task_id})["result"]["status"] == "working"
    with engine.begin() as c:
        c.execute(text("UPDATE code_task_runs SET status = 'completed', phase = 'done', "
                       "pull_request_url = 'https://github.com/acme/app/pull/7' WHERE id = :r"),
                  {"r": run_id})
    done = client.rpc("tasks/get", {"taskId": task_id})["result"]
    assert done["status"] == "completed", done
    assert done["result"]["structuredContent"]["run"]["pull_request_url"].endswith("/pull/7")
    assert done["result"]["isError"] is False

    # Another caller who may not read builds learns nothing about the handle.
    nobody = world.person("Nobody")
    tokens["test-nobody"] = world.key_ctx(nobody, ("project:read",))
    hidden = client.rpc("tasks/get", {"taskId": task_id}, bearer="test-nobody")
    assert hidden["error"]["code"] == -32011
    # The tasks extension is a 2026-07-28 method: a legacy request does not reach it.
    legacy = http.post("/mcp", json={"jsonrpc": "2.0", "id": 9, "method": "tasks/get",
                                     "params": {"taskId": task_id}},
                       headers={"Authorization": "Bearer test-pat"}).json()
    assert legacy["error"]["code"] == -32601


def test_a_a_deployed_agent_answers_input_required_and_completes_on_the_retry(
        engine, world, mcp, parts):  # noqa: F811
    """MRTR on a synchronous call (no tasks extension): `input_required` with a sealed
    requestState; the retry carries the answer (validated with step-up) and the run's result.
    The run joins the caller's trace (`_meta.traceparent`)."""
    http, tokens, _ = mcp
    project = world.project_a
    key = _key("triage")
    _agent(engine, project, key)
    _deploy(engine, project, key, "prod", 1)
    tokens["test-fresh"] = world.ctx(world.owner, mfa=True)
    other = world.person("Other Operator")
    world.bind(other, "operator")
    tokens["test-other"] = world.key_ctx(other, ("agent:invoke", "decision:approve"))
    trace_id, caller_span = uuid.uuid4().hex, uuid.uuid4().hex[:16]
    client = ModernMCPClient(http, "test-fresh", tasks=False,
                             traceparent=f"00-{trace_id}-{caller_span}-01")
    tool = f"agent_{key}"
    assert tool in [t["name"] for t in client.rpc("tools/list")["result"]["tools"]]
    stop, th = _worker(engine, project)
    try:
        args = {"input": "close #311 as a duplicate", "timeout": 30}
        first = client.rpc("tools/call", {"name": tool, "arguments": args})["result"]
        assert first["resultType"] == "input_required", first
        (req_key, req), = first["inputRequests"].items()
        assert req_key.startswith("decision:") and req["params"]["mode"] == "form"
        assert set(req["params"]["requestedSchema"]["properties"]) >= {"verdict", "note"}
        state = first["requestState"]
        # A tampered state, or the same state from another caller, is refused.
        assert client.rpc("tools/call", {"name": tool, "arguments": args,
                                         "requestState": state[:-2] + "xx",
                                         "inputResponses": {}})["error"]["code"] == -32602
        assert client.rpc("tools/call", {"name": tool, "arguments": args,
                                         "requestState": state, "inputResponses": {}},
                          bearer="test-other")["error"]["code"] == -32602
        answer = {req_key: {"action": "accept", "content": {"verdict": "approve",
                                                            "note": "duplicate of #298"}}}
        done = client.rpc("tools/call", {"name": tool, "arguments": args,
                                         "requestState": state,
                                         "inputResponses": answer})["result"]
    finally:
        stop.set()
        th.join(5)
    assert done["resultType"] == "complete" and done["isError"] is False, done
    body = done["structuredContent"]
    assert body["status"] == "success" and body["output"] == "v1: closed after approval"
    assert parts.effects == [("close_issue", "#311")], "the approved call ran exactly once"
    with engine.connect() as c:
        root = c.execute(text("SELECT trace_id, parent_span_id FROM trace_spans WHERE run_id = :r "
                              "AND kind = 'run' ORDER BY started_at LIMIT 1"),
                         {"r": body["run_id"]}).mappings().first()
    assert root is not None and root["trace_id"] == trace_id
    assert root["parent_span_id"] == caller_span, "the run is a child of the MCP caller's span"


def test_a_legacy_2024_11_05_clients_are_unchanged():
    from boss_engine.mcp.server import MCPServer
    from boss_engine.tools.registry import ToolRegistry, ToolSpec

    reg = ToolRegistry()
    reg.register(ToolSpec("zeta", "z", {"type": "object"}, lambda ctx, **k: {"n": 1},
                          kind="read", permission="system:read"))
    reg.register(ToolSpec("alpha", "a", {"type": "object"}, lambda ctx, **k: {"n": 2},
                          kind="read", permission="system:read"))
    srv = MCPServer(ctx=None, project_id="p", registry=reg)
    out = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})["result"]
    assert out["protocolVersion"] == "2024-11-05" and out["capabilities"] == {"tools": {}}
    asked = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-11-25"}})["result"]
    assert asked["protocolVersion"] == "2025-11-25"
    listed = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]
    assert set(listed) == {"tools"} and [t["name"] for t in listed["tools"]] == ["alpha", "zeta"]
    called = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                         "params": {"name": "zeta", "arguments": {}}})["result"]
    assert "resultType" not in called and json.loads(called["content"][0]["text"])["n"] == 1


# ── (b) A2A v1.0 ───────────────────────────────────────────────────────────────────────────────

def _a2a(client: TestClient, agent_id: str, method: str, params: dict, *, version: str = "1.0"):
    resp = client.post(f"/a2a/{agent_id}", json={"jsonrpc": "2.0", "id": uuid.uuid4().hex,
                                                 "method": method, "params": params},
                       headers={"A2A-Version": version})
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
def vaulted(monkeypatch):
    """The agent's private key and the client's push credentials are vault items (a test key)."""
    import base64

    from boss_engine.identity import vault
    from boss_engine.services import agent_keys

    monkeypatch.setenv("BOSS_MASTER_KEY", base64.b64encode(b"p" * 32).decode())
    monkeypatch.delenv("BOSS_MASTER_KEY_FILE", raising=False)
    vault.reset_master_key_cache()
    agent_keys.clear_cache_for_tests()
    yield
    vault.reset_master_key_cache()
    agent_keys.clear_cache_for_tests()


def test_b_an_a2a_client_verifies_the_card_meets_input_required_resumes_and_is_pushed(
        engine, world, parts, monkeypatch, vaulted):  # noqa: F811
    from apps.api.app.main import app
    from boss_engine.protocols import agent_card
    from boss_engine.services import a2a_server, agent_keys, mcp_oauth

    project = world.project_a
    client = TestClient(app, raise_server_exceptions=False)
    owner_session = world.ctx(world.owner, mfa=True)
    portal = _sa(world, engine, "Support portal", ("agent:invoke",))
    reader = _sa(world, engine, "Read only", ("agent:read",))
    monkeypatch.setattr(mcp_oauth, "guard_url", lambda url, what="": url)
    pushed: list = []
    monkeypatch.setattr(a2a_server, "POST", lambda url, content, headers: (
        pushed.append({"url": url, "body": json.loads(content), "headers": dict(headers)}), 200)[1])

    # D11: an agent with a write tool may never be exposed (no isolation yet).
    writer = _key("writer")
    _agent(engine, project, writer, spec_tools={"skills": ["github.create_pr_sandbox"]})
    _deploy(engine, project, writer, "prod", 1)
    with world.acting_as(owner_session):
        refused = client.put(f"/v1/agents/{writer}/deployments/prod/a2a",
                             json={"project_id": project, "enabled": True})
    assert refused.status_code == 409 and "read-only" in refused.json()["detail"], refused.text

    # A read-only agent: off by default; no signing key → still refused.
    key = _key("triage")
    _agent(engine, project, key)
    _deploy(engine, project, key, "prod", 1)
    with engine.connect() as c:
        agent_id = c.execute(text("SELECT agent_id FROM roles WHERE project_id = :p AND "
                                  "role_key = :r"), {"p": project, "r": key}).scalar()
    assert agent_id
    assert client.get(f"/a2a/{agent_id}/.well-known/agent-card.json").status_code == 404
    with world.acting_as(owner_session):
        nokey = client.put(f"/v1/agents/{key}/deployments/prod/a2a",
                           json={"project_id": project, "enabled": True})
    assert nokey.status_code == 409 and "signing key" in nokey.json()["detail"]
    with engine.begin() as c:
        agent_keys.create_key(c, project, key, actor="ceo")
    with world.acting_as(owner_session):
        on = client.put(f"/v1/agents/{key}/deployments/prod/a2a",
                        json={"project_id": project, "enabled": True})
    assert on.status_code == 200 and on.json()["a2a"] is True, on.text

    # THE CARD: public, signed with the agent's own key, verified against its JWKS.
    card = client.get(f"/a2a/{agent_id}/.well-known/agent-card.json").json()
    jwks = client.get(f"/v1/agents/{agent_id}/jwks").json()
    header = agent_card.verify_card(card, jwks)
    assert header["alg"] == "EdDSA" and header["jku"].endswith(f"/v1/agents/{agent_id}/jwks")
    assert card["supportedInterfaces"][0]["url"].endswith(f"/a2a/{agent_id}")
    assert card["capabilities"] == {"streaming": True, "pushNotifications": True}
    same = client.get("/.well-known/agent-card.json", params={"agent_id": agent_id}).json()
    assert agent_card.verify_card(same, jwks)
    tampered = {**card, "description": "Transfers money."}
    with pytest.raises(agent_card.CardSignatureError):
        agent_card.verify_card(tampered, jwks)

    stop, th = _worker(engine, project)
    try:
        # Auth: a key without agent:invoke is refused; the portal's is not.
        with world.acting_as(reader):
            denied = client.post(f"/a2a/{agent_id}", json={"jsonrpc": "2.0", "id": 1,
                                                           "method": "GetTask",
                                                           "params": {"id": "x"}})
        assert denied.status_code in (403, 404)
        with world.acting_as(portal):
            assert _a2a(client, agent_id, "GetTask", {"id": "x"},
                        version="2.0")["error"]["code"] == -32009
            sent = _a2a(client, agent_id, "SendMessage", {
                "message": {"messageId": "m1", "role": "ROLE_USER",
                            "parts": [{"text": "close #311 as a duplicate"}]},
                "configuration": {"taskPushNotificationConfig": {
                    "url": "https://client.example/a2a-hook", "token": "tok-123",
                    "authentication": {"scheme": "Bearer", "credentials": "hook-secret"}}}})
        task = sent["result"]["task"]
        assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED", sent
        data = next(p["data"] for p in task["status"]["message"]["parts"] if "data" in p)
        decision_id = data["decisions"][0]["decision_id"]
        assert data["decisions"][0]["approve_url"].endswith(f"/decisions/{decision_id}")
        assert pushed and pushed[-1]["body"]["task"]["status"]["state"] == \
            "TASK_STATE_INPUT_REQUIRED"

        # The service account cannot approve in-band (people approve, with step-up).
        with world.acting_as(portal):
            sa_try = _a2a(client, agent_id, "SendMessage", {"message": {
                "messageId": "m2", "role": "ROLE_USER", "taskId": task["id"],
                "parts": [{"data": {"decision_id": decision_id, "verdict": "approve"}}]}})
        assert sa_try["error"]["code"] == -32010, sa_try
        # A person approves in BOSS (the Decision API, a fresh second factor) and the client
        # resumes the task with a message on it.
        with world.acting_as(owner_session):
            approved = client.post(f"/v1/decisions/{decision_id}/approve",
                                   json={"actor": "x", "note": "duplicate of #298"})
        assert approved.status_code == 200, approved.text
        with world.acting_as(portal):
            resumed = _a2a(client, agent_id, "SendMessage", {
                "message": {"messageId": "m3", "role": "ROLE_USER", "taskId": task["id"],
                            "parts": [{"text": "approved — please continue"}]}})
        state = resumed["result"]["task"]["status"]["state"]
        assert state in ("TASK_STATE_WORKING", "TASK_STATE_COMPLETED"), resumed
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not any(
                p["body"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED" for p in pushed):
            time.sleep(0.2)
        final = [p for p in pushed if p["body"]["task"]["status"]["state"] ==
                 "TASK_STATE_COMPLETED"]
        assert len(final) == 1, pushed
        hook = final[0]
        assert hook["url"] == "https://client.example/a2a-hook"
        assert hook["headers"]["Content-Type"] == "application/a2a+json"
        assert hook["headers"]["Authorization"] == "Bearer hook-secret"
        assert hook["headers"]["X-A2A-Notification-Token"] == "tok-123"
        assert hook["body"]["task"]["artifacts"][0]["parts"][0]["text"] == \
            "v1: closed after approval"
        with world.acting_as(portal):
            got = _a2a(client, agent_id, "GetTask", {"id": task["id"]})["result"]
            configs = _a2a(client, agent_id, "ListTaskPushNotificationConfigs",
                           {"taskId": task["id"]})["result"]["configs"]
        assert got["status"]["state"] == "TASK_STATE_COMPLETED"
        assert got["contextId"] == task["contextId"]
        assert "token" not in json.dumps(configs) and "hook-secret" not in json.dumps(configs)
        assert parts.effects == [("close_issue", "#311")]

        # STREAMING: the Task first, then status updates, closing at the end.
        with world.acting_as(portal):
            with client.stream("POST", f"/a2a/{agent_id}", json={
                    "jsonrpc": "2.0", "id": "s1", "method": "SendStreamingMessage",
                    "params": {"message": {"messageId": "m4", "role": "ROLE_USER",
                                           "contextId": task["contextId"],
                                           "parts": [{"text": "sweep the backlog"}]}}},
                    headers={"A2A-Version": "1.0"}) as streamed:
                assert streamed.headers["content-type"].startswith("text/event-stream")
                events = [json.loads(line[len("data: "):]) for line in streamed.iter_lines()
                          if line.startswith("data: ")]
        assert "task" in events[0]["result"], events
        assert events[-1]["result"]["statusUpdate"]["status"]["state"] == "TASK_STATE_COMPLETED"
        # CANCEL: an ended task is not cancelable.
        with world.acting_as(portal):
            assert _a2a(client, agent_id, "CancelTask",
                        {"id": task["id"]})["error"]["code"] == -32002
    finally:
        stop.set()
        th.join(5)

    # Turning it off closes the endpoint and the card at once.
    with world.acting_as(owner_session):
        assert client.put(f"/v1/agents/{key}/deployments/prod/a2a",
                          json={"project_id": project, "enabled": False}).status_code == 200
    assert client.get(f"/a2a/{agent_id}/.well-known/agent-card.json").status_code == 404


# ── helpers ────────────────────────────────────────────────────────────────────────────────────

def _fake_start_code_task(engine, project: str):
    """The code-task pipeline's entry, minus the sandbox: the run row a build starts with."""
    def start(connection, *, project_id, title, instructions, **_kw):
        run_id = f"crun_{uuid.uuid4().hex[:12]}"
        connection.execute(text(
            "INSERT INTO code_task_runs (id, project_id, title, status, phase, spec) VALUES "
            "(:i, :p, :t, 'running', 'queued', CAST(:s AS JSONB))"),
            {"i": run_id, "p": project_id, "t": title,
             "s": json.dumps({"instructions": instructions})})
        return run_id
    return start


def _only_code_task(engine, project: str) -> str:
    with engine.connect() as c:
        return c.execute(text("SELECT id FROM code_task_runs WHERE project_id = :p "
                              "ORDER BY created_at DESC LIMIT 1"), {"p": project}).scalar()


def _decision(engine, decision_id: str) -> dict:
    with engine.connect() as c:
        return dict(c.execute(text("SELECT status, approved_by FROM decisions WHERE id = :d"),
                              {"d": decision_id}).mappings().one())
