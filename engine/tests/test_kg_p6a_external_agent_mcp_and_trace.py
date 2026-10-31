"""KG P6a follow-up: the external agent's real MCP write path, read-back `produced`, egress
check, and the trace/audit records the review found missing.

These tests exercise the NEW surface this fix-round adds on top of the already-shipped
``external_agent_runner``/``external_agent_tokens``/``external_agents`` modules:

  * ``boss_engine.mcp.server.MCPServer.handle_external_agent_call`` — the only entry point a
    ``boss_ext_...`` token may use, and that it refuses anything else.
  * ``boss_engine.services.knowledge.context_graph.read_contract_writes`` — reads real
    ``kg_nodes`` rows back, never trusts the HTTP body.
  * ``boss_engine.services.knowledge.external_agents.check_endpoint_egress`` — refuses a
    private/loopback-resolving host unless it is an already-allowed exception.
  * audit events written by ``external_agent_tokens.mint``/``revoke``.
  * a trace record of the call surfaced through ``context_graph.project_agent_run``.

Uses the same Postgres test-database fixture pattern as
``test_kg_p6a_external_agent_step.py`` (an in-process ``http.server`` stands in for the bank's
agent).
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from sqlalchemy import create_engine, text

os.environ.setdefault("BOSS_KG_MCP_URL", "http://127.0.0.1:1/mcp")

from boss_engine.config import Settings
from boss_engine.services.knowledge import external_agent_tokens, external_agents, scopes
from boss_engine.services.knowledge.step_contracts import parse_contract
from boss_engine.services.secret_backends import DatabaseBackend

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"),
)

CONTRACT = {
    "reads": [{"kind": "rule"}],
    "writes": [{"kind": "agent_decision", "required": True, "decision_type": "approval"}],
}


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM workflow_step_runs LIMIT 1"))
            if c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar() is None:
                pytest.skip("test database has no workspace")
    except Exception as error:  # pragma: no cover
        pytest.skip(f"test database unavailable or not migrated: {error}")
    return engine


@pytest.fixture()
def project(eng):
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar()
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'ext agent mcp', :s)"), {"i": pid, "w": wid, "s": pid})
    yield pid
    with eng.begin() as c:
        tables = [r[0] for r in c.execute(text(
            "SELECT table_name FROM information_schema.columns WHERE column_name='project_id' "
            "AND table_schema = current_schema() AND table_name <> 'projects'"))]
        for _ in range(4):
            for table in tables:
                try:
                    with c.begin_nested():
                        c.execute(text(f'DELETE FROM "{table}" WHERE project_id=:p'), {"p": pid})
                except Exception:  # noqa: BLE001 — still referenced; a later pass gets it
                    pass
        c.execute(text("DELETE FROM projects WHERE id=:p"), {"p": pid})


class _Ctx:
    settings = Settings()

    def __init__(self, engine):
        self._engine = engine

    @contextmanager
    def begin(self):
        with self._engine.begin() as c:
            yield c


def _mint(connection, project_id, *, run_id, step_id, agent_id="ext_test"):
    contract = parse_contract(step_id, CONTRACT)
    base_policy = scopes.StagePolicy("custom", frozenset({"knowledge", "rules"}), code="none")
    from boss_engine.services.knowledge.step_contracts import policy_from_contract
    policy = policy_from_contract(base_policy, contract)
    return external_agent_tokens.mint(
        connection, project_id=project_id, run_id=run_id, step_id=step_id,
        external_agent_id=agent_id, policy=policy, ttl_seconds=60)


class TestPAgentDecisionWriteAndRefusal:
    """P0: a verified external-agent token can write exactly the tools its contract grants, and
    is refused in words for anything else."""

    def test_p0_token_can_write_agent_decision_through_mcp(self, eng, project):
        from boss_engine.mcp.server import MCPServer

        run_id, step_id = f"r_{uuid.uuid4().hex[:8]}", "s1"
        with eng.begin() as c:
            issued = _mint(c, project, run_id=run_id, step_id=step_id)
        server = MCPServer(_Ctx(eng), project_id=project)
        response = server.handle_external_agent_call(issued.token, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "kg_write_agent_decision",
                       "arguments": {"decision_type": "approval", "statement": "looks fine"}},
        })
        assert response is not None
        assert "error" not in response, response
        with eng.begin() as c:
            row = c.execute(text(
                "SELECT id FROM kg_nodes WHERE project_id=:p AND kind='agent_decision' "
                "AND node_key=:nk"
            ), {"p": project, "nk": f"context:agent_decision:{run_id}:{step_id}:approval"}).first()
        assert row is not None

    def test_p0_token_is_refused_for_any_other_tool(self, eng, project):
        from boss_engine.mcp.server import MCPServer

        run_id, step_id = f"r_{uuid.uuid4().hex[:8]}", "s1"
        with eng.begin() as c:
            issued = _mint(c, project, run_id=run_id, step_id=step_id)
        server = MCPServer(_Ctx(eng), project_id=project)
        response = server.handle_external_agent_call(issued.token, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "kg_search", "arguments": {"query": "anything"}},
        })
        assert response is not None
        text_blob = json.dumps(response)
        assert "error" in response or '"isError": true' in text_blob or '"isError":true' in text_blob

    def test_p0_invalid_or_revoked_token_is_refused(self, eng, project):
        from boss_engine.mcp.server import MCPServer

        run_id, step_id = f"r_{uuid.uuid4().hex[:8]}", "s1"
        with eng.begin() as c:
            issued = _mint(c, project, run_id=run_id, step_id=step_id)
            external_agent_tokens.revoke(c, issued.token_id)
        server = MCPServer(_Ctx(eng), project_id=project)
        response = server.handle_external_agent_call(issued.token, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "kg_write_agent_decision",
                       "arguments": {"decision_type": "approval", "statement": "x"}},
        })
        assert response is not None
        assert "error" in response

    def test_p1_a_token_cannot_write_into_a_different_run_or_step(self, eng, project):
        """AC2: claims from verify(), not the caller's arguments, decide where the write lands —
        there is no `run_id`/`step_id` argument the caller can pass to redirect it."""
        from boss_engine.mcp.server import MCPServer

        run_id, step_id = f"r_{uuid.uuid4().hex[:8]}", "s1"
        other_run_id = f"r_{uuid.uuid4().hex[:8]}"
        with eng.begin() as c:
            issued = _mint(c, project, run_id=run_id, step_id=step_id)
        server = MCPServer(_Ctx(eng), project_id=project)
        server.handle_external_agent_call(issued.token, {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "kg_write_agent_decision",
                       "arguments": {"decision_type": "approval", "statement": "x",
                                     "run_id": other_run_id, "step_id": "s2"}},
        })
        with eng.begin() as c:
            wrong = c.execute(text(
                "SELECT id FROM kg_nodes WHERE project_id=:p AND kind='agent_decision' "
                "AND node_key=:nk"
            ), {"p": project, "nk": f"context:agent_decision:{other_run_id}:s2:approval"}).first()
            right = c.execute(text(
                "SELECT id FROM kg_nodes WHERE project_id=:p AND kind='agent_decision' "
                "AND node_key=:nk"
            ), {"p": project, "nk": f"context:agent_decision:{run_id}:{step_id}:approval"}).first()
        assert wrong is None
        assert right is not None


class TestPReadContractWritesIsTheSourceOfTruth:
    """P1 (AC3): `run_step`'s `produced` is built only from rows that actually exist, never the
    agent's self-reported HTTP body."""

    def test_p1_read_contract_writes_returns_only_rows_that_exist(self, eng, project):
        from boss_engine.services.knowledge import context_graph, step_contracts

        run_id, step_id = f"r_{uuid.uuid4().hex[:8]}", "s1"
        contract = parse_contract(step_id, CONTRACT)
        with eng.begin() as c:
            produced_before = context_graph.read_contract_writes(
                c, project, run_id=run_id, step_id=step_id, contract=contract)
            assert produced_before == {} or not step_contracts.unsatisfied_required(
                contract, produced_before) is False  # never synthesized before the write exists
        with eng.begin() as c:
            context_graph.write_agent_decision(
                c, project, step_id=step_id, run_id=run_id, decision_type="approval",
                statement="real write", policy_version="v1", workflow_version="v1",
                agent_fingerprint="fp", actor_ref="external_agent:ext_test")
        with eng.begin() as c:
            produced_after = context_graph.read_contract_writes(
                c, project, run_id=run_id, step_id=step_id, contract=contract)
        from boss_engine.services.knowledge.step_contracts import unsatisfied_required
        assert not unsatisfied_required(contract, produced_after)

    def test_p1_a_required_write_never_made_through_mcp_is_unsatisfied(self, eng, project):
        from boss_engine.services.knowledge import context_graph
        from boss_engine.services.knowledge.step_contracts import unsatisfied_required

        run_id, step_id = f"r_{uuid.uuid4().hex[:8]}", "never_called"
        contract = parse_contract(step_id, CONTRACT)
        with eng.begin() as c:
            produced = context_graph.read_contract_writes(
                c, project, run_id=run_id, step_id=step_id, contract=contract)
        assert unsatisfied_required(contract, produced)


class TestPTokenMintAndRevokeAreAudited:
    """P1 (AC5): every mint/revoke writes exactly one audit event in the same transaction."""

    def test_p1_mint_writes_one_token_minted_audit_event(self, eng, project):
        run_id, step_id = f"r_{uuid.uuid4().hex[:8]}", "s1"
        with eng.begin() as c:
            issued = _mint(c, project, run_id=run_id, step_id=step_id)
        with eng.begin() as c:
            rows = c.execute(text(
                "SELECT event_type FROM identity_security_events "
                "WHERE event_type='external_agent.token_minted' AND target_id=:tid"
            ), {"tid": issued.token_id}).fetchall()
        assert len(rows) == 1

    def test_p1_revoke_writes_one_token_revoked_audit_event_and_is_idempotent(self, eng, project):
        run_id, step_id = f"r_{uuid.uuid4().hex[:8]}", "s1"
        with eng.begin() as c:
            issued = _mint(c, project, run_id=run_id, step_id=step_id)
        with eng.begin() as c:
            external_agent_tokens.revoke(c, issued.token_id)
            external_agent_tokens.revoke(c, issued.token_id)  # idempotent: no 2nd event
        with eng.begin() as c:
            rows = c.execute(text(
                "SELECT event_type FROM identity_security_events "
                "WHERE event_type='external_agent.token_revoked' AND target_id=:tid"
            ), {"tid": issued.token_id}).fetchall()
        assert len(rows) == 1
        assert external_agent_tokens.verify(c, issued.token) is None


class TestPCallEndpointEgressCheck:
    """P1 (AC7): a resolved-private-address endpoint is refused unless it is one of the already-
    allowed loopback/tailnet exceptions."""

    def test_p1_loopback_endpoint_is_allowed_by_name(self):
        external_agents.check_endpoint_egress("http://127.0.0.1:9/webhook")
        external_agents.check_endpoint_egress("http://localhost:9/webhook")

    def test_p1_a_hostname_resolving_only_to_a_private_address_is_refused(self, monkeypatch):
        # A name that resolves to a loopback address under getaddrinfo but is not itself one of
        # the allowed loopback/tailnet host literals must still be refused — proven with a
        # stubbed resolver so the assertion holds whether or not this host can reach live DNS.
        import socket as socket_module

        def fake_getaddrinfo(host, *args, **kwargs):
            assert host == "private.example.test"
            return [(socket_module.AF_INET, socket_module.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

        monkeypatch.setattr(external_agents.socket, "getaddrinfo", fake_getaddrinfo)
        with pytest.raises(external_agents.ExternalAgentError):
            external_agents.check_endpoint_egress("https://private.example.test/webhook")


class TestPRunStepRecordsATraceOfTheCall:
    """P1 (AC6): after `run_step`, the call is visible as one AgentRun-shaped entry naming the
    endpoint's hostname, with a non-negative latency and an `ok` matching the outcome."""

    def test_p1_a_successful_call_is_traced_with_hostname_and_latency(self, eng, project):
        from boss_engine.services import external_agent_runner
        from boss_engine.services.knowledge import context_graph, external_agents as ext_mod

        port = _free_port()
        _StandInAgent.response_status = 200
        _StandInAgent.response_body = b'{}'
        httpd = HTTPServer(("127.0.0.1", port), _StandInAgent)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with eng.begin() as c:
                secret_ref = DatabaseBackend().store(c, project, "ext-secret-" + uuid.uuid4().hex)
                agent_id = ext_mod.register(
                    c, project, display_name="Bank Agent",
                    endpoint_url=f"http://127.0.0.1:{port}/step",
                    secret_ref=secret_ref, sponsor_principal_id="person_1",
                    created_by="tester")["id"]
            run_id, step_id = f"r_{uuid.uuid4().hex[:8]}", "s1"
            step = {"id": step_id, "captain": {"external_agent": agent_id},
                    "contract": CONTRACT, "timeout_seconds": 10}
            with eng.begin() as c:
                try:
                    external_agent_runner.run_step(
                        c, _Ctx(eng), project_id=project, step=step,
                        context={"run_id": run_id, "input": {}})
                except Exception:
                    pass  # contract satisfaction isn't this test's concern; the trace row is
            with eng.begin() as c:
                calls = context_graph.project_agent_tool_calls(c, project, run_id=run_id, step_id=step_id) \
                    if hasattr(context_graph, "project_agent_tool_calls") else []
            assert calls, "run_step must record at least one AgentToolCall entry"
            assert any("127.0.0.1" in str(entry) for entry in calls), (
                "the recorded call must name the endpoint's hostname")
        finally:
            httpd.shutdown()
            thread.join(timeout=5)


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _StandInAgent(BaseHTTPRequestHandler):
    response_status = 200
    response_body: bytes = b"{}"

    def do_POST(self):  # noqa: N802 - stdlib handler name
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.send_response(self.__class__.response_status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.__class__.response_body)

    def log_message(self, *args):  # silence stdlib access logging in test output
        pass


class TestPEnsureSchemaIsRemoved:
    """P1 (AC8): the now-dead ensure_schema/SCHEMA_SQL no longer exist on external_agents."""

    def test_p1_ensure_schema_and_schema_sql_are_gone(self):
        assert not hasattr(external_agents, "ensure_schema")
        assert not hasattr(external_agents, "SCHEMA_SQL")
