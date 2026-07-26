"""Tool registry + built-in tool-set tests (no DB)."""
from __future__ import annotations

import pytest

from boss_engine.tools import build_default_registry
from boss_engine.tools.registry import ToolError, ToolRegistry, ToolSpec


def test_default_registry_shape():
    reg = build_default_registry()
    assert len(reg.names()) == 15
    assert len(reg.specs(kind="read")) == 10
    assert len(reg.specs(kind="write")) == 5
    # the capabilities the user explicitly asked the agent to cover
    for name in (
        "project_status", "list_running_work", "get_task_preview",
        "microvm_status", "query_db", "create_code_task", "retry_code_task",
    ):
        assert name in reg.names()


def test_schema_shapes():
    reg = build_default_registry()
    for s in reg.openai_schema():
        assert s["type"] == "function"
        assert set(s["function"]) >= {"name", "description", "parameters"}
    for s in reg.mcp_schema():
        assert set(s) >= {"name", "description", "inputSchema"}


def test_read_only_schema_excludes_writes():
    reg = build_default_registry()
    read_names = {s["name"] for s in reg.mcp_schema(kind="read")}
    assert "create_code_task" not in read_names
    assert "project_status" in read_names


def test_invoke_normalizes_result_and_defaults():
    reg = ToolRegistry()
    reg.register(ToolSpec("echo", "echo", {"type": "object"}, lambda ctx, **k: {"v": k["args"].get("x")}))
    out = reg.invoke("echo", None, project_id="p", actor="a", args={"x": 7})
    assert out == {"v": 7, "ok": True, "tool": "echo"}


def test_invoke_wraps_handler_exception():
    reg = ToolRegistry()

    def boom(ctx, **k):
        raise ValueError("nope")

    reg.register(ToolSpec("boom", "boom", {"type": "object"}, boom))
    out = reg.invoke("boom", None, project_id="p", actor="a", args={})
    assert out["ok"] is False and "ValueError" in out["error"]


def test_unknown_tool_raises():
    reg = ToolRegistry()
    with pytest.raises(ToolError):
        reg.get("missing")


def test_duplicate_registration_rejected():
    reg = ToolRegistry()
    reg.register(ToolSpec("t", "d", {"type": "object"}, lambda ctx, **k: {}))
    with pytest.raises(ValueError):
        reg.register(ToolSpec("t", "d", {"type": "object"}, lambda ctx, **k: {}))
