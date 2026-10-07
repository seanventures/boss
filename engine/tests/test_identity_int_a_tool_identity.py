"""int-a · Every tool call carries the VERIFIED caller identity (IDENTITY.md §6.4, §7.4, §7.14).

  - The MCP server decides each call as the identity its TRANSPORT authenticated: the stdio
    session's BOSS key (re-verified per call), or the HTTP request's bearer. Never a tool
    argument; with none, enforce refuses (fail closed).
  - In-process agent work runs under the run token: the agent loop for a leased job acts for the
    JOB's principal and grant (not the worker that called in) and every loop tool is decided
    against the run token; a workflow step runs under the run's RECORDED authority.

The MCP half needs no database; the run-token half uses the identity-only throwaway database
(BOSS_IDENTITY_TEST_DATABASE_URL). Each test fails if the protection it names is removed.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.identity import authz, lineage
from boss_engine.identity.authz import Decision, DenyReason
from boss_engine.identity.enforcement import LEDGER
from boss_engine.identity.principal import AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus
from boss_engine.mcp import server as mcp_server
from boss_engine.tools import registry as registry_module
from boss_engine.tools.registry import ToolRegistry, ToolSpec

ALICE = AuthContext(Principal("usr_a", PrincipalKind.USER, "org_x", PrincipalStatus.ACTIVE, "A"),
                    AuthMethod.API_KEY)


class _Ctx:
    durable_workspace_configured = True


@pytest.fixture
def enforced(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "authz_mode", "enforce")
    monkeypatch.setattr(settings, "authz_router_modes", "")
    LEDGER.clear()
    seen: list = []

    def check(ctx, permission, scope, **_):
        seen.append((ctx.principal.id, permission))
        if ctx.principal.id == "usr_a":
            return Decision.allow(permission, scope)
        return Decision.deny(permission, scope, DenyReason.MISSING_PERMISSION)

    monkeypatch.setattr(authz, "check", check)
    calls: list = []

    def handler(ctx, **kw):
        acting = lineage.current()
        calls.append({"tool_auth": registry_module.CURRENT_TOOL_AUTH.get(), "args": kw["args"],
                      "acting": acting.principal_id if acting else None})
        return {"done": True}

    reg = ToolRegistry()
    reg.register(ToolSpec("deploy", "d", {"type": "object"}, handler, kind="write", permission="deployment:create"))
    yield reg, calls, seen
    LEDGER.clear()


def _call(server, name="deploy", arguments=None):
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": arguments or {}}})


def _payload(resp) -> dict:
    import json

    return json.loads(resp["result"]["content"][0]["text"])


# ---- MCP: stdio ---------------------------------------------------------------------------------------------

def test_mcp_stdio_decides_every_call_as_the_sessions_verified_identity(enforced):
    reg, calls, seen = enforced
    server = mcp_server.MCPServer(_Ctx(), project_id="p1", registry=reg, identity=lambda: ALICE)
    out = _payload(_call(server, arguments={"auth": "usr_root", "actor": "usr_root", "_identity": {"x": 1}}))
    assert out["done"] is True and seen == [("usr_a", "deployment:create")]
    # The handler ran with the transport's identity as the tool identity and the acting principal.
    assert calls[0]["tool_auth"] is ALICE and calls[0]["acting"] == "usr_a"
    assert registry_module.CURRENT_TOOL_AUTH.get() is None                  # reset after the call


def test_mcp_stdio_without_a_verified_identity_is_refused_under_enforce(enforced):
    reg, calls, _ = enforced
    for identity in (None, lambda: None, lambda: (_ for _ in ()).throw(RuntimeError("revoked"))):
        server = mcp_server.MCPServer(_Ctx(), project_id="p1", registry=reg, identity=identity)
        out = _payload(_call(server))
        assert out["ok"] is False and out["error"] == "identity_unavailable"
    assert calls == []
    # An ambient identity from elsewhere in the process never leaks into an unidentified call.
    token = registry_module.CURRENT_TOOL_AUTH.set(ALICE)
    try:
        server = mcp_server.MCPServer(_Ctx(), project_id="p1", registry=reg)
        assert _payload(_call(server))["error"] == "identity_unavailable" and calls == []
    finally:
        registry_module.CURRENT_TOOL_AUTH.reset(token)


def test_mcp_authenticate_transport_ignores_anything_but_a_boss_credential(monkeypatch):
    from boss_engine.identity import authn

    called: list = []
    monkeypatch.setattr(authn, "authenticate", lambda creds, **k: called.append(creds) or ALICE)
    assert mcp_server.authenticate_transport({}) is None
    assert mcp_server.authenticate_transport({"Authorization": "Bearer shared-mcp-token"}) is None
    assert mcp_server.authenticate_transport({"Authorization": "Basic Ym9zczp4"}) is None
    assert called == []                                       # no bridge, no legacy key ever consulted
    key = authn.mint_api_key("pat")
    assert mcp_server.authenticate_transport({"Authorization": f"Bearer {key}"}) is ALICE
    assert called[0].path == "/mcp" and called[0].cookies == {}


# ---- MCP: HTTP ----------------------------------------------------------------------------------------------

def test_mcp_http_decides_each_request_as_its_bearer_and_the_shared_token_identifies_nobody(enforced, monkeypatch):
    from fastapi.testclient import TestClient

    from boss_engine.identity import authn

    reg, calls, seen = enforced
    good, bad = authn.mint_api_key("pat"), authn.mint_api_key("pat")
    monkeypatch.setattr(mcp_server, "authenticate_transport",
                        lambda headers, **k: ALICE if headers.get("authorization") == f"Bearer {good}" else None)
    # A session identity is never used over HTTP: each request's own bearer decides.
    server = mcp_server.MCPServer(_Ctx(), project_id="p1", registry=reg, http_token="shared-token",
                                  identity=lambda: ALICE)
    client = TestClient(server.asgi_app())
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "deploy", "arguments": {}}}
    ok = client.post("/mcp", json=body, headers={"Authorization": f"Bearer {good}"})
    assert _payload(ok.json())["done"] is True and calls[-1]["tool_auth"] is ALICE
    # The shared token identifies nobody: under enforce the transport refuses it outright (M8).
    shared = client.post("/mcp", json=body, headers={"Authorization": "Bearer shared-token"})
    assert shared.status_code == 401 and len(calls) == 1
    assert client.post("/mcp", json=body, headers={"Authorization": f"Bearer {bad}"}).status_code == 401
    assert client.post("/mcp", json=body).status_code == 401


# ---- in-process runs: the run token, not the worker ----------------------------------------------------

@pytest.fixture()
def env(monkeypatch):
    from ._identity_agents_support import build_env

    e = build_env(monkeypatch)
    with e.db() as c:
        c.execute(text("INSERT INTO roles (id, role_key, agent_id) VALUES ('r-atlas', 'atlas', :a), "
                       "('r-forge', 'forge', :f)"), {"a": e.atlas, "f": e.forge})
    return e


WORKER = lineage.Acting(principal_id="sys_worker", org_id="org_boss")


def _job(env, grant_id: str) -> str:
    who = lineage.Acting(principal_id=env.alice, org_id="org_boss", grant_id=grant_id)
    payload, cols = lineage.stamp_job_row({"k": 1}, who)
    import json

    with env.db() as c:
        c.execute(text("INSERT INTO jobs (id, project_id, kind, payload, on_behalf_of, grant_id, chain) VALUES "
                       "('job_1', :p, 'openai.plan', CAST(:pl AS JSONB), :on_behalf_of, :grant_id, CAST(:chain AS JSONB))"),
                  {"p": env.project, "pl": json.dumps(payload), **cols})
    return "job_1"


def test_a_worker_driven_run_acts_for_the_jobs_principal_and_decides_tools_as_its_run_token(env):
    from boss_engine.identity import catalogue, runs

    g = env.grant()
    job = _job(env, g.id)
    with lineage.acting(WORKER):
        with runs.delegated(env.engine, kind="ag", entity_id="arun1", project_id=env.project, role_key="atlas",
                            scope=catalogue.delegable(), job_id=job) as ctx:
            assert ctx is not None and ctx.run_token.sub == env.alice and ctx.run_token.grant_id == g.id
            assert registry_module.CURRENT_TOOL_AUTH.get() is ctx              # tools decided as the run
            assert lineage.current().chain and lineage.current().principal_id == env.alice
            _, cols = lineage.stamp_job_row({"next": 1})                       # work it queues carries the chain
            assert cols["on_behalf_of"] == env.alice and env.atlas in cols["chain"]
        assert lineage.current() == WORKER and registry_module.CURRENT_TOOL_AUTH.get() is None


def test_a_person_never_borrows_another_persons_recorded_authority(env):
    from boss_engine.identity import catalogue, runs

    g = env.grant()
    job = _job(env, g.id)
    mallory = lineage.Acting(principal_id="usr_mallory", org_id="org_other")
    with lineage.acting(mallory):
        with runs.delegated(env.engine, kind="ag", entity_id="arun2", project_id=env.project, role_key="atlas",
                            scope=catalogue.delegable(), job_id=job) as ctx:
            assert ctx is None
        recorded = lineage.Acting(principal_id=env.alice, org_id="org_boss", grant_id=g.id)
        with runs.on_behalf(env.engine, recorded) as person:
            assert person is None


def test_the_agent_loops_tools_are_decided_against_the_run_token(env):
    from boss_engine.identity import catalogue, runs
    from boss_engine.services import agent_loop

    for name in list(agent_loop._TOOL_FNS) + list(agent_loop._WRITE_FNS):     # every loop tool is declared
        assert catalogue.is_known(agent_loop.AGENT_TOOL_PERMISSIONS[name]), name
    g = env.grant()
    maps = dict(mcp_map={"mcp_x": ({}, {})}, repo_map={}, skill_map={"skill_y": {}}, on_demand_loader=None)
    with env.db() as c:
        assert agent_loop._identity_refusal(c, env.project, "open_incident", **maps) is None   # no run: unchanged
    with lineage.acting(WORKER), runs.delegated(env.engine, kind="ag", entity_id="arun3", project_id=env.project,
                                                role_key="atlas", scope=catalogue.delegable(),
                                                job_id=_job(env, g.id)) as ctx:
        assert ctx is not None
        with env.db() as c:
            assert agent_loop._identity_refusal(c, env.project, "list_repo_files", **maps) is None
            refused = agent_loop._identity_refusal(c, env.project, "open_incident", **maps)
            assert refused["error"] == "forbidden" and refused["permission"] == "reliability:respond"
            assert agent_loop._identity_refusal(c, env.project, "run_shell", **maps)["error"] == "forbidden"
            assert agent_loop._identity_refusal(c, env.project, "mcp_x", **maps)["permission"] == "skill:invoke"
            assert agent_loop._identity_refusal(c, env.project, "made_up_tool", **maps)["reason"] == "undeclared_tool"


def test_a_workflow_step_runs_under_the_runs_recorded_authority_and_fails_closed_when_it_ends(env):
    from boss_engine.identity import cascade
    from boss_engine.identity.delegation import CascadeTrigger
    from boss_engine.services import workflow_capability_runner as runner

    from ._identity_agents_support import session_ctx

    g = env.grant()
    with env.db() as c:
        c.execute(text("INSERT INTO workflow_runs (id, status, on_behalf_of, grant_id, authority_org_id, authority_run_id) "
                       "VALUES ('wr1', 'running', :a, :g, 'org_boss', 'run_wf:wr1')"), {"a": env.alice, "g": g.id})

    class StepCtx:
        engine = env.engine

    with lineage.acting(WORKER):
        with env.db() as c, runner._step_authority(StepCtx(), c, env.project, {"run_id": "wr1"}, {"id": "publish"},
                                                   "atlas") as ctx:
            assert ctx.run_token.task.run_id == "run_wf:wr1/step:publish" and ctx.run_token.sub == env.alice
            assert registry_module.CURRENT_TOOL_AUTH.get() is ctx
        # A step with no agent Passport acts as the person (live), never as the worker.
        with env.db() as c, runner._step_authority(StepCtx(), c, env.project, {"run_id": "wr1"}, {"id": "notify"},
                                                   "ceo") as person:
            assert person.principal.id == env.alice and person.run_token is None
            assert registry_module.CURRENT_TOOL_AUTH.get() is person and lineage.current().grant_id == g.id
        # A run with no recorded authority is unchanged.
        with env.db() as c, runner._step_authority(StepCtx(), c, env.project, {"run_id": "wr_none"}, {"id": "x"},
                                                   "atlas") as nothing:
            assert nothing is None and lineage.current() == WORKER
    with env.db() as c:
        cascade.revoke_cascade(CascadeTrigger.GRANT_REVOKED, g.id, actor=session_ctx(env.alice), reason="x", db=c)
    with lineage.acting(WORKER), env.db() as c, pytest.raises(runner.CapabilityExecutionError):
        with runner._step_authority(StepCtx(), c, env.project, {"run_id": "wr1"}, {"id": "publish"}, "atlas"):
            pass
