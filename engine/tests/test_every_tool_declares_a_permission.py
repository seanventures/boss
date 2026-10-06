"""Every engine/MCP tool declares a catalogue permission, or is still on the shrinking list.

IDENTITY.md §6.4 and §15.4 R2. A tool's `permission` is checked against the catalogue when its spec
is built (a misspelt key fails at import); API-surface tools inherit their route's declaration.
Every tool declares; the shrinking list emptied and was deleted (stream S2), and a ToolSpec with
no permission cannot be built.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from boss_engine.identity.catalogue import CatalogueError, is_known
from boss_engine.tools import build_default_registry
from boss_engine.tools.registry import ToolSpec

def test_every_tool_declares_and_the_shrinking_list_is_gone():
    specs = build_default_registry().specs()
    assert [s.name for s in specs if not s.permission and not s.inherits_route_permission] == []
    assert len(specs) >= 114
    assert not (Path(__file__).resolve().parents[2] / "tests" / "identity"
                / "undeclared_mcp_tools.txt").exists()


def test_a_tool_without_a_permission_cannot_be_built():
    with pytest.raises(CatalogueError, match="declares no permission"):
        ToolSpec("x", "x", {"type": "object"}, lambda *a, **k: {})


def test_every_declared_tool_names_a_catalogue_permission():
    for spec in build_default_registry().specs():
        if spec.permission:
            assert is_known(spec.permission), spec.name


def test_a_tool_with_an_unknown_permission_cannot_be_built():
    with pytest.raises(CatalogueError):
        ToolSpec("x", "x", {"type": "object"}, lambda *a, **k: {}, permission="tools:everything")
