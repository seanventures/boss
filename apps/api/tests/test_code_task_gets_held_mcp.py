"""A code task run as an agent gets exactly that agent's MCP servers (skills-mcp E6, test 10)."""
from __future__ import annotations

import json
import os
import stat
import uuid

from sqlalchemy import text

PROJECT = "boss-dogfood"
SECRET = "ctx7-key-" + "b" * 16 + "4321"


def _server(client, engine, name, *, tier=0, tools=("resolve", "docs", "publish")):
    made = client.post("/v1/mcp/servers", json={
        "project_id": PROJECT, "actor": "ceo", "name": name, "url": f"https://{name}.example/mcp",
        "risk_tier": tier, "auth": {"type": "bearer", "secret": SECRET}}).json()
    client.post(f"/v1/mcp/servers/{made['id']}/review", json={"project_id": PROJECT,
                                                               "actor": "ceo"})
    with engine.begin() as c:
        c.execute(text("UPDATE mcp_servers SET reachable = true, tools = CAST(:t AS JSONB) "
                       "WHERE id = :id"),
                  {"t": json.dumps([{"name": t} for t in tools]), "id": made["id"]})
    return made


def test_the_harness_gets_the_agents_servers_with_ask_tools_disallowed(client, engine):
    from boss_engine.config import get_settings
    from boss_engine.services.code_task_mcp import prepare

    name = f"docs{uuid.uuid4().hex[:5]}"
    server = _server(client, engine, name)
    other = _server(client, engine, f"other{uuid.uuid4().hex[:5]}")
    client.post("/v1/role-mcp-servers", json={"project_id": PROJECT, "actor": "ceo",
                                              "role_key": "backend_engineer",
                                              "mcp_server_id": server["id"],
                                              "tools": ["resolve", "publish"]})
    client.patch(f"/v1/mcp/servers/{server['id']}", params={"project_id": PROJECT},
                 json={"tool_risk_overrides": {"publish": 2}})
    with engine.begin() as c:
        harness = prepare(c, get_settings(), PROJECT, "backend_engineer")
    try:
        assert harness is not None and harness.path
        mode = stat.S_IMODE(os.stat(harness.path).st_mode)
        assert mode == 0o600
        config = json.loads(open(harness.path).read())["mcpServers"]
        assert name in config and other["name"] not in config
        assert config[name]["headers"]["Authorization"] == f"Bearer {SECRET}"
        argv = harness.argv()
        assert argv[:3] == ["--strict-mcp-config", "--mcp-config", harness.path]
        blocked = argv[argv.index("--disallowedTools") + 1].split(",")
        # publish asks (tier 2), docs is outside the grant; resolve is the only one allowed.
        assert f"mcp__{name}__publish" in blocked and f"mcp__{name}__docs" in blocked
        assert f"mcp__{name}__resolve" not in blocked
    finally:
        harness.cleanup()
    assert not os.path.exists(harness.path), "the file with the token outlived the run"


def test_an_agent_holding_nothing_gets_no_config(engine):
    from boss_engine.config import get_settings
    from boss_engine.services.code_task_mcp import prepare

    with engine.begin() as c:
        c.execute(text("DELETE FROM role_mcp_servers WHERE project_id=:p AND role_key='cto'"),
                  {"p": PROJECT})
        harness = prepare(c, get_settings(), PROJECT, "cto")
        assert prepare(c, get_settings(), PROJECT, None) is None
    assert harness.argv() == ["--strict-mcp-config"]


def test_the_claude_harness_is_started_with_the_flags(monkeypatch):
    from boss_engine.config import get_settings
    from boss_engine.services.code_agent import make_code_agent
    from boss_engine.services.code_task_mcp import HarnessMcp

    harness = HarnessMcp(path="/tmp/x/mcp.json", servers=["docs"], disallowed=["mcp__docs__w"])
    agent = make_code_agent(get_settings().model_copy(update={"code_agent_engine":
                                                              "claude_code"}), mcp=harness)
    # PacedCodeAgent delegates attribute reads to the agent it wraps.
    assert agent.mcp_harness is harness
