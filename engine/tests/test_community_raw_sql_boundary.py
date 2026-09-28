"""Community chat and direct tools cannot query or mutate unscoped database rows."""

from types import SimpleNamespace

import pytest

from boss_engine.services.agent_loop import _tool_query_db
from boss_engine.tools.builtin import build_default_registry, build_discuss_read_registry


class _NoDatabase:
    def __init__(self, edition: str):
        self.settings = SimpleNamespace(boss_edition=edition)
        self.calls = 0

    def begin(self):
        self.calls += 1
        raise AssertionError("raw SQL reached the database")


@pytest.mark.parametrize("edition", ["community", "oss", "local", "unrecognized"])
def test_community_registries_hide_raw_sql_but_direct_invocation_still_blocks(edition):
    discuss = build_discuss_read_registry(edition=edition)
    assert "query_db" not in discuss.names()
    assert "write_db" not in discuss.names()

    ctx = _NoDatabase(edition)
    registry = build_default_registry()
    read = registry.invoke(
        "query_db", ctx, project_id="own", actor="member",
        args={"sql": "SELECT * FROM projects"},
    )
    write = registry.invoke(
        "write_db", ctx, project_id="own", actor="member",
        args={"sql": "UPDATE projects SET name='changed' WHERE id='other'",
              "reason": "test"}, operator_confirmed=True,
    )
    assert read["ok"] is False and "Community" in read["error"]
    assert write["ok"] is False and "Community" in write["error"]
    assert ctx.calls == 0


@pytest.mark.parametrize("edition", ["community", "oss", "local", "unrecognized"])
def test_legacy_agent_query_db_blocks_before_execute(edition):
    class Connection:
        def execute(self, *args, **kwargs):
            raise AssertionError("raw SQL reached the database")

    result = _tool_query_db(
        Connection(), "own", SimpleNamespace(boss_edition=edition),
        {"sql": "SELECT * FROM projects"},
    )
    assert "raw SQL is unavailable" in result["error"]


@pytest.mark.parametrize("tool,args", [
    ("query_db", {"sql": "SELECT * FROM projects"}),
    ("write_db", {"sql": "DELETE FROM projects WHERE id='other'", "reason": "test"}),
])
def test_paid_default_registry_requires_explicit_local_admin_call(tool, args):
    ctx = _NoDatabase("enterprise_self_hosted")
    result = build_default_registry().invoke(
        tool, ctx, project_id="own", actor="mcp", args=args,
        operator_confirmed=True,
    )
    assert result["ok"] is False
    assert "local admin CLI" in result["error"]
    assert ctx.calls == 0


@pytest.mark.parametrize("edition", ["cloud", "enterprise", "enterprise_cloud",
                                     "enterprise_self_hosted", None])
def test_paid_project_pinned_chat_holds_unscoped_sql(edition):
    registry = build_discuss_read_registry(edition=edition)
    names = registry.names()
    assert "query_db" not in names
    assert "write_db" not in names
    assert "project_status" in names
    assert "create_deployment_target" in names
    offered = registry.invoke(
        "propose_action", _NoDatabase(edition or "enterprise_self_hosted"),
        project_id="own", actor="member",
        args={"tool": "write_db", "args": {"sql": "DELETE FROM projects"}},
    )
    assert offered["ok"] is False
