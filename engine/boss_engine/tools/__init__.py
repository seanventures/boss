"""The BOSS engine tool layer.

    from boss_engine.tools import build_default_registry
    reg = build_default_registry()
    reg.openai_schema()          # for the agent's tool-calling loop
    reg.mcp_schema()             # for the MCP server
    reg.invoke("project_status", ctx, project_id=..., actor=..., args={})
"""
from __future__ import annotations

from .builtin import build_default_registry, build_discuss_read_registry
from .registry import ToolError, ToolRegistry, ToolSpec

__all__ = [
    "ToolRegistry",
    "ToolSpec",
    "ToolError",
    "build_default_registry",
    "build_discuss_read_registry",
]
