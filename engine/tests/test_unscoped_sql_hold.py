"""Project-scoped model and connector paths cannot use control-plane raw SQL."""

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from boss_engine.agent.engine_agent import EngineAgent
from boss_engine.mcp import MCPServer
from boss_engine.services import agent_loop, debug_session, workflow_capability_runner
from boss_engine.tools.builtin import build_default_registry


_SETTINGS = SimpleNamespace(boss_edition="enterprise_self_hosted")


class _NoDatabase:
    settings = _SETTINGS
    durable_workspace_configured = True
    raw_sql_admin_authorized = False

    def begin(self):
        raise AssertionError("unscoped SQL reached the database")


@pytest.mark.parametrize("tool", ["query_db", "write_db"])
def test_engine_agent_does_not_advertise_or_dispatch_raw_sql(tool):
    agent = EngineAgent(_NoDatabase())
    assert tool not in agent.registry.names()
    # A custom registry cannot turn a model call into the local CLI admin path.
    custom = EngineAgent(_NoDatabase(), registry=build_default_registry())
    assert tool in custom.registry.names()
    result = custom._run_tool(tool, {"sql": "SELECT * FROM projects"},
                              project_id="own", actor="agent", allow_writes=True,
                              pending=[])
    assert result["ok"] is False
    assert "raw SQL" in result["error"]


def test_custom_engine_registry_still_hides_raw_sql_from_model_schema():
    class CaptureAdapter:
        tools = None

        def create_chat_completion(self, messages, *, tools=None):
            self.tools = tools
            return ({"raw": {"role": "assistant", "content": "Done."},
                     "content": "Done.", "tool_calls": []}, {})

    adapter = CaptureAdapter()
    agent = EngineAgent(_NoDatabase(), registry=build_default_registry(), adapter=adapter)
    agent.chat(project_id="own", message="status", max_steps=2)
    names = {schema["function"]["name"] for schema in adapter.tools}
    assert "query_db" not in names
    assert "write_db" not in names
    assert "project_status" in names


@pytest.mark.parametrize("tool", ["query_db", "write_db"])
def test_native_mcp_does_not_advertise_or_dispatch_raw_sql(tool):
    server = MCPServer(_NoDatabase(), project_id="own")
    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert tool not in {item["name"] for item in listed["result"]["tools"]}
    assert server._tool_allowed(tool) is False
    result = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                            "params": {"name": tool, "arguments": {"sql": "SELECT 1"}}})
    assert result["result"]["isError"] is True


def test_unbounded_legacy_agent_hides_and_refuses_sql():
    assert "query_db" not in {
        item["function"]["name"] for item in agent_loop._native_tool_schemas(_SETTINGS)
    }
    assert "query_db" not in {item["name"] for item in agent_loop.describe_native_tools()}
    result = agent_loop._dispatch(None, "own", _SETTINGS, "query_db",
                                  {"sql": "SELECT * FROM projects"})
    assert "raw SQL" in result["error"]


@pytest.mark.parametrize("tool", ["query_db", "write_db"])
def test_workflow_step_refuses_raw_sql(tool):
    with pytest.raises(workflow_capability_runner.CapabilityExecutionError,
                       match="raw SQL"):
        workflow_capability_runner._invoke_tool(
            _NoDatabase(), tool, "own", "workflow", {"sql": "SELECT 1"}
        )


def test_investigation_callback_never_executes_project_scoped_sql():
    result = debug_session.run_db(
        _NoDatabase(), {"project_id": "own", "allow_fix": True},
        "SELECT * FROM projects",
    )
    assert result["ok"] is False
    assert "raw SQL" in result["error"]


class _AdminRows:
    def mappings(self):
        return self

    def all(self):
        return [{"answer": 1}]


class _AdminConnection:
    def execute(self, statement):
        assert str(statement).lower() == "select 1 as answer"
        return _AdminRows()


class _LocalAdmin:
    settings = _SETTINGS
    raw_sql_admin_authorized = True

    @contextmanager
    def begin(self):
        yield _AdminConnection()


def test_explicit_local_admin_query_still_works():
    registry = build_default_registry()
    result = registry.invoke("query_db", _LocalAdmin(), project_id="own",
                             actor="cli", args={"sql": "SELECT 1 AS answer"})
    assert result["rows"] == [{"answer": 1}]
    denied = registry.invoke("query_db", _LocalAdmin(), project_id="own",
                              actor="mcp", args={"sql": "SELECT 1 AS answer"})
    assert denied["ok"] is False


def test_cli_call_grants_sql_authority_only_for_that_explicit_command(monkeypatch, capsys):
    from boss_engine import __main__ as cli

    ctx = _LocalAdmin()
    ctx.raw_sql_admin_authorized = False
    monkeypatch.setattr(cli, "_ctx", lambda: ctx)
    args = SimpleNamespace(name="query_db", project="own",
                           args=json.dumps({"sql": "SELECT 1 AS answer"}), confirm=False)
    assert cli._cmd_call(args) == 0
    assert json.loads(capsys.readouterr().out)["rows"] == [{"answer": 1}]
    assert ctx.raw_sql_admin_authorized is True
