"""KG P6a follow-up (operator P1): an external-agent token can READ the evidence its contract
grants ("reads within scope") through ``MCPServer.handle_external_agent_call`` — only the
contract's read kinds, and refused in words for a kind the contract never named.
"""
from __future__ import annotations

import os
import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text

os.environ.setdefault("BOSS_KG_MCP_URL", "http://127.0.0.1:1/mcp")

from boss_engine.config import Settings
from boss_engine.services.knowledge import context_graph, external_agent_tokens, scopes
from boss_engine.services.knowledge.step_contracts import parse_contract, policy_from_contract

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"),
)

# Grants reading `rule` nodes and writing an agent_decision.
CONTRACT = {
    "reads": [{"kind": "rule"}],
    "writes": [{"kind": "agent_decision", "required": True, "decision_type": "approval"}],
}


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM kg_nodes LIMIT 1"))
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
                       "VALUES (:i, :w, 'ext agent reads', :s)"), {"i": pid, "w": wid, "s": pid})
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
                except Exception:  # noqa: BLE001
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


def _mint(connection, project_id, *, run_id, step_id):
    contract = parse_contract(step_id, CONTRACT)
    base = scopes.StagePolicy("custom", frozenset({"knowledge", "rules"}), code="none", max_nodes=50)
    policy = policy_from_contract(base, contract)
    return external_agent_tokens.mint(
        connection, project_id=project_id, run_id=run_id, step_id=step_id,
        external_agent_id="ext_reader", policy=policy, ttl_seconds=60)


def _seed_rule(connection, project_id, node_key, statement):
    connection.execute(text(
        "INSERT INTO kg_nodes (id, project_id, kind, node_key, name, attrs) "
        "VALUES (:id, :p, 'rule', :nk, :nk, :a)"
    ), {"id": f"n_{uuid.uuid4().hex[:16]}", "p": project_id, "nk": node_key,
        "a": '{"statement": "%s"}' % statement})


def test_reads_within_scope_succeed(eng, project):
    from boss_engine.mcp.server import MCPServer

    run_id, step_id = f"r_{uuid.uuid4().hex[:8]}", "s1"
    rule_key = f"rule:{uuid.uuid4().hex[:8]}"
    with eng.begin() as c:
        _seed_rule(c, project, rule_key, "retain records for seven years")
        issued = _mint(c, project, run_id=run_id, step_id=step_id)
    server = MCPServer(_Ctx(eng), project_id=project)

    found = server.handle_external_agent_call(issued.token, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "kg_find", "arguments": {"kind": "rule"}}})
    assert found is not None and "error" not in found, found

    got = server.handle_external_agent_call(issued.token, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "kg_get_node", "arguments": {"node_key": rule_key}}})
    assert got is not None and "error" not in got, got


def test_a_kind_outside_the_contract_is_refused(eng, project):
    from boss_engine.mcp.server import MCPServer

    run_id, step_id = f"r_{uuid.uuid4().hex[:8]}", "s1"
    with eng.begin() as c:
        issued = _mint(c, project, run_id=run_id, step_id=step_id)
    server = MCPServer(_Ctx(eng), project_id=project)

    response = server.handle_external_agent_call(issued.token, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "kg_find", "arguments": {"kind": "code_symbol"}}})
    assert response is not None
    assert "error" in response, response
