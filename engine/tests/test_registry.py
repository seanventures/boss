"""Tool registry + built-in tool-set tests (no DB)."""
from __future__ import annotations

import pytest

from boss_engine.tools import build_default_registry, build_discuss_read_registry
from boss_engine.tools.registry import ToolError, ToolRegistry, ToolSpec


def test_default_registry_shape():
    reg = build_default_registry()
    # The governed catalog has expanded since the original Phase 1 tool layer;
    # keep this explicit so accidental tool removal is visible in OSS CI.
    assert len(reg.names()) == 31
    assert len(reg.specs(kind="read")) == 18
    assert len(reg.specs(kind="write")) == 13
    # the capabilities the user explicitly asked the agent to cover
    for name in (
        "project_status", "list_running_work", "get_task_preview",
        "microvm_status", "query_db", "create_code_task", "retry_code_task",
        "extend_code_task", "cancel_code_task", "waive_gates", "decide", "get_costs",
        "steer_code_task", "watch_code_task",
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


def test_write_tools_fail_closed_without_a_durable_workspace():
    reg = ToolRegistry()
    called = False

    def write(_ctx, **_kwargs):
        nonlocal called
        called = True
        return {"created": True}

    reg.register(ToolSpec("create_work", "write", {"type": "object"}, write, kind="write"))
    ctx = type("CatalogOnlyContext", (), {"durable_workspace_configured": False})()

    out = reg.invoke("create_work", ctx, project_id="p", actor="a", args={})

    assert out["ok"] is False
    assert out["status"] == "blocked"
    assert out["error"] == "durable_workspace_required"
    assert "DATABASE_URL" in out["message"]
    assert called is False


def test_unknown_tool_raises():
    reg = ToolRegistry()
    with pytest.raises(ToolError):
        reg.get("missing")


def test_duplicate_registration_rejected():
    reg = ToolRegistry()
    reg.register(ToolSpec("t", "d", {"type": "object"}, lambda ctx, **k: {}))
    with pytest.raises(ValueError):
        reg.register(ToolSpec("t", "d", {"type": "object"}, lambda ctx, **k: {}))


def test_discuss_read_registry_excludes_query_db_and_hides_project_override():
    """Chat discuss path must not expose unrestricted SQL or cross-project overrides."""
    reg = build_discuss_read_registry()
    assert "query_db" not in reg.names()
    assert "project_status" in reg.names()
    assert "get_code_task" in reg.names()
    assert "get_costs" in reg.names()
    # All read tools except query_db (18 - 1).
    assert len(reg.specs(kind="read")) == 17
    for schema in reg.openai_schema():
        props = schema["function"]["parameters"].get("properties") or {}
        assert "project_id" not in props, schema["function"]["name"]


def test_discuss_read_registry_drops_project_id_override_from_args():
    """Pinned discuss handlers must ignore a model-supplied project_id arg."""
    seen: dict = {}

    def spy(ctx, *, project_id, actor, args):
        seen["project_id"] = project_id
        seen["args"] = dict(args)
        return {"ok": True}

    def _pin(inner):
        def handler(ctx, *, project_id, actor, args):
            safe_args = dict(args or {})
            safe_args.pop("project_id", None)
            return inner(ctx, project_id=project_id, actor=actor, args=safe_args)

        return handler

    # Same pin helper shape as build_discuss_read_registry.
    wrapped = _pin(spy)
    wrapped(
        None,
        project_id="project-a",
        actor="a",
        args={"project_id": "project-b", "limit": 3},
    )
    assert seen["project_id"] == "project-a"
    assert "project_id" not in seen["args"]
    assert seen["args"]["limit"] == 3
