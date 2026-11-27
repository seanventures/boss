"""KG P6a: the External agent step (engine).

A bank's own HTTP-hosted agent runs one step of a BOSS workflow under BOSS's contract: BOSS
mints a short-lived, scope-bound KG token for the one execution, calls the agent synchronously,
validates its typed output, and revokes the token when the step ends. These tests exercise the
new surface the PRD/tech plan name directly:

  * ``services.knowledge.external_agents`` — registration + the one plain_reason/scrub helpers.
  * ``services.knowledge.external_agent_tokens`` — mint/verify/revoke of the per-execution token.
  * ``services.external_agent_runner`` — the synchronous call (``run_step``).
  * ``services.workflows`` — spec validation for the new ``external_agent`` step kind.

An in-process ``http.server`` stands in for the bank's agent, as the PRD's acceptance criteria
require ("an in-process HTTP agent standing in for a bank's agent").
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from sqlalchemy import create_engine, text

# Must be set before `Settings()` is constructed below (`_Ctx.settings = Settings()` at class
# body time): the real KG MCP URL this feature now requires a step to be handed (no invented
# "/mcp/kg" default — AC "refuse the step if none is configured").
os.environ.setdefault("BOSS_KG_MCP_URL", "http://127.0.0.1:1/mcp")

from boss_engine.config import Settings
from boss_engine.services.knowledge import scopes
from boss_engine.services.knowledge.step_contracts import parse_contract, unsatisfied_required
from boss_engine.services.secret_backends import DatabaseBackend

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"),
)

CONTRACT = {
    "reads": [{"kind": "rule"}],
    "writes": [{"kind": "agent_decision", "required": True}],
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
                       "VALUES (:i, :w, 'ext agent step', :s)"), {"i": pid, "w": wid, "s": pid})
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


class _StandInAgent(BaseHTTPRequestHandler):
    """A bank's agent: reasons through a scripted response, set per test on the class."""

    response_status = 200
    response_body: bytes = b"{}"
    received: list[dict] = []

    def do_POST(self):  # noqa: N802 - stdlib handler name
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            self.__class__.received.append({
                "body": json.loads(raw.decode()),
                "auth_header": self.headers.get("Authorization", ""),
            })
        except Exception:
            self.__class__.received.append({"body": None, "auth_header": self.headers.get("Authorization", "")})
        self.send_response(self.__class__.response_status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.__class__.response_body)

    def log_message(self, *args):  # silence stdlib access logging in test output
        pass


@pytest.fixture()
def stand_in_agent():
    _StandInAgent.response_status = 200
    _StandInAgent.response_body = b"{}"
    _StandInAgent.received = []
    server = HTTPServer(("127.0.0.1", 0), _StandInAgent)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, _StandInAgent
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _register_agent(connection, project_id: str, endpoint_url: str, *, secret_value: str = None) -> dict:
    """Registers a bank agent whose `secret_ref` resolves, through the real secret-refs
    resolver, to `secret_value` (a fresh one when not given) — never the reference itself."""
    from boss_engine.services.knowledge import external_agents

    secret_value = secret_value or f"secretval_{uuid.uuid4().hex[:24]}"
    ref_id = f"sref_{uuid.uuid4().hex[:16]}"
    ref_key = f"p6a/{ref_id}"
    DatabaseBackend(engine=connection.engine).put(ref_key, secret_value)
    connection.execute(text(
        "INSERT INTO secret_refs (id, project_id, scope_type, scope_id, ref_kind, ref_key, "
        "secret_kind, storage_backend, status, created_by) "
        "VALUES (:id, :p, 'project', :p, 'database', :key, 'api_token', 'database', 'active', 'op')"
    ), {"id": ref_id, "p": project_id, "key": ref_key})

    return external_agents.register(
        connection, project_id,
        display_name="Bank Agent",
        endpoint_url=endpoint_url,
        secret_ref=ref_id,
        sponsor_principal_id=f"person_{uuid.uuid4().hex[:8]}",
        created_by="op",
    )


class TestTheHappyPathRunsEndToEnd:
    """P0: a contract workflow step named to an external agent runs: the agent is called with
    a scoped token, its typed output satisfies the contract's required write, and the token is
    revoked once the step ends (AC1)."""

    def test_run_step_calls_the_agent_and_revokes_its_token_after(self, eng, project, stand_in_agent):
        from boss_engine.services import external_agent_runner
        from boss_engine.services.knowledge import context_graph, external_agent_tokens

        server, handler = stand_in_agent
        with eng.begin() as c:
            agent = _register_agent(c, project, f"http://127.0.0.1:{server.server_port}")

        # The stand-in agent drives the real path: on receiving the step payload it writes an
        # AgentDecision into the Context Graph (as a bank agent would through its KG token), then
        # acks a bare `{}`. The step now passes ONLY because that real `kg_nodes` row exists — the
        # self-reported HTTP body is never trusted for contract satisfaction.
        pid = project

        class _WritesThroughTheGraph(handler):
            def do_POST(self):  # noqa: N802 - stdlib handler name
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode())
                self.__class__.received.append(
                    {"body": body, "auth_header": self.headers.get("Authorization", "")})
                with eng.begin() as wc:
                    context_graph.write_agent_decision(
                        wc, pid, step_id=body["step_id"], run_id=body["run_id"],
                        decision_type="proceed", statement="looks fine", policy_version="",
                        workflow_version="", agent_fingerprint="",
                        actor_ref=f"external_agent:{agent['id']}")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

        server.RequestHandlerClass = _WritesThroughTheGraph

        ctx = _Ctx(eng)
        step = {"id": "s1", "kind": "external_agent",
                "captain": {"external_agent": agent["id"]}, "contract": CONTRACT,
                "timeout_seconds": 10}
        with eng.begin() as c:
            result = external_agent_runner.run_step(
                c, ctx, project_id=project,
                step=step, context={"run_id": f"wfr_{uuid.uuid4().hex[:8]}"})

        assert result["kind"] == "external_agent"
        contract = parse_contract("s1", CONTRACT)
        # `produced` was read back from the real kg_nodes row, not the HTTP body.
        assert unsatisfied_required(contract, result["produced"]) == []
        assert len(handler.received) == 1
        sent = handler.received[0]["body"]
        token = sent["kg"]["token"]
        assert token  # a real token was handed to the agent

        with eng.begin() as c:
            # The call is over; the per-execution token must not still verify.
            assert external_agent_tokens.verify(c, token) is None


class TestTheTokenIsScopedToExactlyTheContract:
    """P1: the per-execution token's scope comes straight from the contract's reads/writes — a
    tool whose layer the contract did not declare is not offered, and the layers are narrowed to
    exactly the declared kinds, never "every kind in the layer" (AC2/AC9)."""

    def test_an_undeclared_layers_tool_is_not_offered_but_a_declared_one_is(self, eng, project):
        from boss_engine.services.knowledge import external_agent_tokens

        contract = parse_contract("s1", CONTRACT)
        policy = scopes.policy_for(step={"id": "s1", "contract": CONTRACT})
        assert policy.kinds == frozenset({"rule"})

        with eng.begin() as c:
            issued = external_agent_tokens.mint(
                c, project_id=project, run_id=f"wfr_{uuid.uuid4().hex[:8]}", step_id="s1",
                external_agent_id=f"ext_{uuid.uuid4().hex[:8]}", policy=policy, ttl_seconds=60)
            claims = external_agent_tokens.verify(c, issued.token)

        assert claims is not None
        # The contract declared only `rule` (layer "rules"): a tool reading `knowledge`
        # (kg_propose_fact) is out of scope, a tool reading `rules` (kg_rules) is in scope.
        assert scopes.offers_tool(claims.policy, "kg_propose_fact") is False
        assert scopes.offers_tool(claims.policy, "kg_rules") is True


class TestARevokedOrEndedTokenIsRefused:
    """P1: a token used after its step has ended is refused — looked up live against the
    revocation record, never accepted on expiry math alone (AC4)."""

    def test_verify_returns_none_once_revoked(self, eng, project):
        from boss_engine.services.knowledge import external_agent_tokens

        policy = scopes.policy_for(step={"id": "s1", "contract": CONTRACT})
        with eng.begin() as c:
            issued = external_agent_tokens.mint(
                c, project_id=project, run_id=f"wfr_{uuid.uuid4().hex[:8]}", step_id="s1",
                external_agent_id=f"ext_{uuid.uuid4().hex[:8]}", policy=policy, ttl_seconds=60)
            assert external_agent_tokens.verify(c, issued.token) is not None
            external_agent_tokens.revoke(c, issued.token_id)
            assert external_agent_tokens.verify(c, issued.token) is None
            # Idempotent: ending an already-ended step must not raise.
            external_agent_tokens.revoke(c, issued.token_id)


class TestAMissingRequiredWriteIsDetected:
    """P1: a step whose typed output omits a required write is caught by the existing
    postcondition check — no new enforcement code for this feature (AC5)."""

    def test_output_missing_the_required_write_fails_the_postcondition(self, eng, project, stand_in_agent):
        from boss_engine.services import external_agent_runner

        server, handler = stand_in_agent
        with eng.begin() as c:
            agent = _register_agent(c, project, f"http://127.0.0.1:{server.server_port}")
        handler.response_body = json.dumps({"produced": {}}).encode()

        ctx = _Ctx(eng)
        step = {"id": "s1", "kind": "external_agent",
                "captain": {"external_agent": agent["id"]}, "contract": CONTRACT,
                "timeout_seconds": 10}
        with eng.begin() as c:
            result = external_agent_runner.run_step(
                c, ctx, project_id=project,
                step=step, context={"run_id": f"wfr_{uuid.uuid4().hex[:8]}"})

        contract = parse_contract("s1", CONTRACT)
        assert unsatisfied_required(contract, result.get("produced") or {}) == ["agent_decision"]


class TestFailuresNeverLeakTheTokenOrTheAuthHeader:
    """P1: a non-2xx response fails the step with a plain reason, and even when the bank's
    endpoint echoes the Authorization header straight back (the canary scenario), neither the
    raised error nor anything the runner returns contains the token or the header (AC6/AC7)."""

    def test_a_500_fails_with_a_plain_reason_that_never_contains_the_secret(self, eng, project, stand_in_agent):
        from boss_engine.services import external_agent_runner
        from boss_engine.services.workflow_capability_runner import CapabilityExecutionError

        server, handler = stand_in_agent
        with eng.begin() as c:
            agent = _register_agent(c, project, f"http://127.0.0.1:{server.server_port}")

        canary = f"canary-{secrets.token_urlsafe(16)}"
        handler.response_status = 500
        # The stand-in echoes back whatever Authorization header it received, mimicking a
        # broken endpoint that leaks the bank's own auth header in its error body.
        handler.response_body = json.dumps({"error": "boom", "echo": canary}).encode()

        ctx = _Ctx(eng)
        step = {"id": "s1", "kind": "external_agent",
                "captain": {"external_agent": agent["id"]}, "contract": CONTRACT,
                "timeout_seconds": 10}
        with eng.begin() as c:
            with pytest.raises(CapabilityExecutionError) as excinfo:
                external_agent_runner.run_step(
                    c, ctx, project_id=project,
                    step=step, context={"run_id": f"wfr_{uuid.uuid4().hex[:8]}"})

        message = str(excinfo.value)
        assert message  # a plain reason was raised, not a bare exception
        sent_auth_header = handler.received[0]["auth_header"] if handler.received else ""
        if sent_auth_header:
            assert sent_auth_header not in message
        assert canary not in message


class TestASuspendedAgentIsRefusedJustLikeAnUnregisteredOne:
    """P1: an agent that exists for this project but is suspended must be refused at run time
    with the SAME plain reason as an agent id that does not exist at all — ``active_for_project``
    is the one gate ``run_step`` checks, and suspension must not leak through it (AC8, Risk #4)."""

    def test_run_step_refuses_a_suspended_agent_with_the_unregistered_wording(self, eng, project, stand_in_agent):
        from boss_engine.services import external_agent_runner
        from boss_engine.services.knowledge import external_agents
        from boss_engine.services.workflow_capability_runner import CapabilityExecutionError

        server, handler = stand_in_agent
        with eng.begin() as c:
            agent = _register_agent(c, project, f"http://127.0.0.1:{server.server_port}")
            external_agents.set_status(c, project, agent["id"], "suspended",
                                        actor_id="op", reason="pause for review")

        ctx = _Ctx(eng)
        step = {"id": "s1", "kind": "external_agent",
                "captain": {"external_agent": agent["id"]}, "contract": CONTRACT,
                "timeout_seconds": 10}
        with eng.begin() as c:
            with pytest.raises(CapabilityExecutionError) as excinfo:
                external_agent_runner.run_step(
                    c, ctx, project_id=project,
                    step=step, context={"run_id": f"wfr_{uuid.uuid4().hex[:8]}"})

        assert str(excinfo.value) == external_agents.plain_reason("unregistered_agent")
        # The agent was never called: it was refused before any HTTP call was made.
        assert handler.received == []


class TestANonJsonOrOversizedResponseIsRejected:
    """P1: the two response shapes the runner must reject before they ever reach a contract
    check — a body that is not valid JSON, and a body that exceeds the size cap while it is
    being streamed, never after it is buffered whole (AC6)."""

    def test_a_non_json_body_fails_with_the_invalid_json_reason(self, eng, project, stand_in_agent):
        from boss_engine.services import external_agent_runner
        from boss_engine.services.knowledge import external_agents
        from boss_engine.services.workflow_capability_runner import CapabilityExecutionError

        server, handler = stand_in_agent
        with eng.begin() as c:
            agent = _register_agent(c, project, f"http://127.0.0.1:{server.server_port}")
        handler.response_body = b"not json"

        ctx = _Ctx(eng)
        step = {"id": "s1", "kind": "external_agent",
                "captain": {"external_agent": agent["id"]}, "contract": CONTRACT,
                "timeout_seconds": 10}
        with eng.begin() as c:
            with pytest.raises(CapabilityExecutionError) as excinfo:
                external_agent_runner.run_step(
                    c, ctx, project_id=project,
                    step=step, context={"run_id": f"wfr_{uuid.uuid4().hex[:8]}"})

        assert str(excinfo.value) == external_agents.plain_reason("invalid_json")

    def test_an_oversized_body_fails_with_the_oversized_body_reason(self, eng, project, stand_in_agent):
        from boss_engine.services import external_agent_runner
        from boss_engine.services.knowledge import external_agents
        from boss_engine.services.workflow_capability_runner import CapabilityExecutionError

        server, handler = stand_in_agent
        with eng.begin() as c:
            agent = _register_agent(c, project, f"http://127.0.0.1:{server.server_port}")
        oversized = json.dumps(
            {"produced": {"agent_decision": {"decision_type": "proceed", "pad": "x" * (260 * 1024)}}}
        ).encode()
        handler.response_body = oversized

        ctx = _Ctx(eng)
        step = {"id": "s1", "kind": "external_agent",
                "captain": {"external_agent": agent["id"]}, "contract": CONTRACT,
                "timeout_seconds": 10}
        with eng.begin() as c:
            with pytest.raises(CapabilityExecutionError) as excinfo:
                external_agent_runner.run_step(
                    c, ctx, project_id=project,
                    step=step, context={"run_id": f"wfr_{uuid.uuid4().hex[:8]}"})

        assert str(excinfo.value) == external_agents.plain_reason("oversized_body")


class TestATimeoutIsAWaitNotAFailureAndStillRevokesTheToken:
    """P1: an endpoint that never answers within ``timeout_seconds`` surfaces as a
    ``CapabilityWaitError`` — not a bare failure — carrying a plain reason and a
    ``wait_seconds``/``kind`` the run loop retries on, and the per-execution token is still
    revoked even though the call never completed (AC6, trace-honesty property)."""

    def test_a_slow_endpoint_raises_capability_wait_error_and_revokes_the_token(
            self, eng, project, stand_in_agent):
        from boss_engine.services import external_agent_runner
        from boss_engine.services.knowledge import external_agents
        from boss_engine.services.workflow_capability_runner import CapabilityWaitError

        server, handler = stand_in_agent

        class _NeverResponds(handler):
            def do_POST(self):  # noqa: N802 - stdlib handler name
                import time

                time.sleep(2)
                super().do_POST()

        server.RequestHandlerClass = _NeverResponds

        with eng.begin() as c:
            agent = _register_agent(c, project, f"http://127.0.0.1:{server.server_port}")

        ctx = _Ctx(eng)
        step = {"id": "s1", "kind": "external_agent",
                "captain": {"external_agent": agent["id"]}, "contract": CONTRACT,
                "timeout_seconds": 1}
        with eng.begin() as c:
            with pytest.raises(CapabilityWaitError) as excinfo:
                external_agent_runner.run_step(
                    c, ctx, project_id=project,
                    step=step, context={"run_id": f"wfr_{uuid.uuid4().hex[:8]}"})

        assert str(excinfo.value) == external_agents.plain_reason("timeout")
        assert excinfo.value.wait_seconds == 30
        assert excinfo.value.kind == "external_agent_timeout"

        # The token minted for this call must have been revoked even though the call itself
        # never completed — a crashed/timed-out step must not strand a live bearer credential.
        with eng.begin() as c:
            revoked_at = c.execute(text(
                "SELECT revoked_at FROM external_agent_kg_tokens WHERE external_agent_id = :a "
                "ORDER BY created_at DESC LIMIT 1"), {"a": agent["id"]}).scalar()
        assert revoked_at is not None


class TestSpecValidationRejectsAMalformedOrUnknownExternalAgentStep:
    """P1: a spec naming `external_agent` with no contract is rejected at validation; a spec
    naming an unregistered agent and one naming an agent registered to a DIFFERENT project are
    both rejected with identical wording, so neither message tells an outsider which case it
    is (AC8, Risk #4)."""

    def test_no_contract_and_cross_tenant_agents_are_rejected_with_identical_wording(self, eng, project):
        from boss_engine.services.workflows import (
            external_agent_assignment_problems,
            external_agent_shape_problems,
        )

        no_contract_step = {"id": "s1", "kind": "external_agent",
                            "captain": {"external_agent": "ext_whatever"}}
        shape_problems = external_agent_shape_problems(no_contract_step)
        assert shape_problems, "a step with no contract must be rejected at validation"

        other_pid = f"p_{uuid.uuid4().hex[:10]}"
        with eng.begin() as c:
            wid = c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar()
            c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                           "VALUES (:i, :w, 'other project', :s)"), {"i": other_pid, "w": wid, "s": other_pid})
            foreign_agent = _register_agent(c, other_pid, "https://bank.example/agent")
        try:
            unregistered_step = {"id": "s1", "kind": "external_agent",
                                 "captain": {"external_agent": f"ext_{uuid.uuid4().hex[:10]}"},
                                 "contract": CONTRACT}
            cross_project_step = {"id": "s1", "kind": "external_agent",
                                  "captain": {"external_agent": foreign_agent["id"]},
                                  "contract": CONTRACT}
            spec_a = {"id": "wf1", "steps": [unregistered_step]}
            spec_b = {"id": "wf1", "steps": [cross_project_step]}
            with eng.begin() as c:
                problems_unregistered = external_agent_assignment_problems(c, project, spec_a)
                problems_cross_project = external_agent_assignment_problems(c, project, spec_b)

            assert problems_unregistered, "an unregistered agent must be rejected"
            assert problems_cross_project, "an agent registered to another project must be rejected"

            # Strip each message's own agent id out, then the two must read identically — an
            # outsider must not be able to tell "unregistered" from "registered elsewhere".
            stripped_unregistered = problems_unregistered[0].replace(
                unregistered_step["captain"]["external_agent"], "<agent>")
            stripped_cross_project = problems_cross_project[0].replace(foreign_agent["id"], "<agent>")
            assert stripped_unregistered == stripped_cross_project
        finally:
            with eng.begin() as c:
                tables = [r[0] for r in c.execute(text(
                    "SELECT table_name FROM information_schema.columns WHERE column_name='project_id' "
                    "AND table_schema = current_schema() AND table_name <> 'projects'"))]
                for _ in range(4):
                    for table in tables:
                        try:
                            with c.begin_nested():
                                c.execute(text(f'DELETE FROM "{table}" WHERE project_id=:p'), {"p": other_pid})
                        except Exception:  # noqa: BLE001
                            pass
                c.execute(text("DELETE FROM projects WHERE id=:p"), {"p": other_pid})


class TestTheAuthHeaderSendsTheSecretsValueNeverItsReference:
    """Operator review of PR #25, item 1: the bank auth header must send the secret VALUE the
    agent's `secret_ref` resolves to — through the real secret-refs resolver — never the bare
    reference string. A reference that no longer resolves must refuse the step, never fall back
    to sending the reference itself as a credential."""

    def test_the_agent_receives_the_resolved_secret_value_not_the_reference(
            self, eng, project, stand_in_agent):
        from boss_engine.services import external_agent_runner

        server, handler = stand_in_agent
        secret_value = f"realsecret_{uuid.uuid4().hex[:24]}"
        with eng.begin() as c:
            agent = _register_agent(c, project, f"http://127.0.0.1:{server.server_port}",
                                     secret_value=secret_value)
        handler.response_body = json.dumps(
            {"produced": {"agent_decision": {"decision_type": "proceed"}}}
        ).encode()

        ctx = _Ctx(eng)
        step = {"id": "s1", "kind": "external_agent",
                "captain": {"external_agent": agent["id"]}, "contract": CONTRACT,
                "timeout_seconds": 10}
        with eng.begin() as c:
            external_agent_runner.run_step(
                c, ctx, project_id=project,
                step=step, context={"run_id": f"wfr_{uuid.uuid4().hex[:8]}"})

        sent_auth = handler.received[0]["auth_header"]
        assert sent_auth == f"Bearer {secret_value}"
        # The reference id itself (what's stored on the agent row) must never be the value sent.
        assert agent["secret_ref"] not in sent_auth

    def test_a_secret_ref_that_does_not_resolve_refuses_the_step_before_any_call(
            self, eng, project, stand_in_agent):
        from boss_engine.services import external_agent_runner
        from boss_engine.services.knowledge import external_agents
        from boss_engine.services.workflow_capability_runner import CapabilityExecutionError

        server, handler = stand_in_agent
        with eng.begin() as c:
            agent = _register_agent(c, project, f"http://127.0.0.1:{server.server_port}")
            # Revoke the underlying secret reference: it no longer resolves to a value.
            c.execute(text("UPDATE secret_refs SET status = 'revoked' WHERE id = :id"),
                      {"id": agent["secret_ref"]})

        ctx = _Ctx(eng)
        step = {"id": "s1", "kind": "external_agent",
                "captain": {"external_agent": agent["id"]}, "contract": CONTRACT,
                "timeout_seconds": 10}
        with eng.begin() as c:
            with pytest.raises(CapabilityExecutionError) as excinfo:
                external_agent_runner.run_step(
                    c, ctx, project_id=project,
                    step=step, context={"run_id": f"wfr_{uuid.uuid4().hex[:8]}"})

        assert str(excinfo.value) == external_agents.plain_reason("secret_unavailable")
        # Never called: the secret must resolve before the agent is ever reached.
        assert handler.received == []


class TestTheStepIsHandedTheRealKgMcpUrlOrRefused:
    """Operator review of PR #25, item 2 (partial): the external agent is handed the real KG
    MCP server's URL from settings, never an invented `/mcp/kg` path — and a step is refused
    outright, before any call is made, when none is configured."""

    def test_the_agent_is_handed_the_configured_kg_mcp_url(self, eng, project, stand_in_agent):
        from boss_engine.services import external_agent_runner

        server, handler = stand_in_agent
        with eng.begin() as c:
            agent = _register_agent(c, project, f"http://127.0.0.1:{server.server_port}")
        handler.response_body = json.dumps(
            {"produced": {"agent_decision": {"decision_type": "proceed"}}}
        ).encode()

        ctx = _Ctx(eng)
        step = {"id": "s1", "kind": "external_agent",
                "captain": {"external_agent": agent["id"]}, "contract": CONTRACT,
                "timeout_seconds": 10}
        with eng.begin() as c:
            external_agent_runner.run_step(
                c, ctx, project_id=project,
                step=step, context={"run_id": f"wfr_{uuid.uuid4().hex[:8]}"})

        sent_mcp_url = handler.received[0]["body"]["kg"]["mcp_url"]
        assert sent_mcp_url == ctx.settings.kg_mcp_url
        assert sent_mcp_url != "/mcp/kg"

    def test_no_configured_kg_mcp_url_refuses_the_step_before_any_call(
            self, eng, project, stand_in_agent):
        from boss_engine.services import external_agent_runner
        from boss_engine.services.knowledge import external_agents
        from boss_engine.services.workflow_capability_runner import CapabilityExecutionError

        server, handler = stand_in_agent
        with eng.begin() as c:
            agent = _register_agent(c, project, f"http://127.0.0.1:{server.server_port}")

        from types import SimpleNamespace

        class _NoKgUrlCtx(_Ctx):
            settings = SimpleNamespace(kg_mcp_url=None)

        ctx = _NoKgUrlCtx(eng)
        step = {"id": "s1", "kind": "external_agent",
                "captain": {"external_agent": agent["id"]}, "contract": CONTRACT,
                "timeout_seconds": 10}
        with eng.begin() as c:
            with pytest.raises(CapabilityExecutionError) as excinfo:
                external_agent_runner.run_step(
                    c, ctx, project_id=project,
                    step=step, context={"run_id": f"wfr_{uuid.uuid4().hex[:8]}"})

        assert str(excinfo.value) == external_agents.plain_reason("kg_mcp_not_configured")
        assert handler.received == []


class TestTheRunnerIsWiredIntoTheStepDispatch:
    """P1: `workflow_capability_runner.execute_step` — the one entry every step kind goes
    through — recognizes `kind: external_agent` and routes it to `external_agent_runner.run_step`
    rather than falling into the agent-work/capability branches below it (design §4.5 G6)."""

    def test_execute_step_dispatches_an_external_agent_step_to_the_runner(
            self, eng, project, stand_in_agent):
        from boss_engine.services.knowledge import context_graph
        from boss_engine.services.workflow_capability_runner import execute_step

        server, handler = stand_in_agent
        with eng.begin() as c:
            agent = _register_agent(c, project, f"http://127.0.0.1:{server.server_port}")

        pid = project

        class _WritesThroughTheGraph(handler):
            def do_POST(self):  # noqa: N802 - stdlib handler name
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode())
                self.__class__.received.append(
                    {"body": body, "auth_header": self.headers.get("Authorization", "")})
                with eng.begin() as wc:
                    context_graph.write_agent_decision(
                        wc, pid, step_id=body["step_id"], run_id=body["run_id"],
                        decision_type="proceed", statement="looks fine", policy_version="",
                        workflow_version="", agent_fingerprint="",
                        actor_ref=f"external_agent:{agent['id']}")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

        server.RequestHandlerClass = _WritesThroughTheGraph

        ctx = _Ctx(eng)
        step = {"id": "s1", "kind": "external_agent",
                "captain": {"external_agent": agent["id"]}, "contract": CONTRACT,
                "timeout_seconds": 10}
        with eng.begin() as c:
            result = execute_step(
                c, ctx, project_id=project, actor="op",
                step=step, context={"run_id": f"wfr_{uuid.uuid4().hex[:8]}"})

        assert result["kind"] == "external_agent"
        contract = parse_contract("s1", CONTRACT)
        assert unsatisfied_required(contract, result["produced"]) == []
        assert len(handler.received) == 1
