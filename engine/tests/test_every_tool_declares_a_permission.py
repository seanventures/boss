"""Every engine/MCP tool declares a catalogue permission, or is still on the shrinking list.

IDENTITY.md §6.4 and §15.4 R2. A tool's `permission` is checked against the catalogue when its spec
is built (a misspelt key fails at import); API-surface tools inherit their route's declaration.
tests/identity/undeclared_mcp_tools.txt lists the tools stream S2 has not declared yet; it may only
shrink, and a tool registered tomorrow must declare.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from boss_engine.identity.catalogue import CatalogueError, is_known
from boss_engine.tools import build_default_registry
from boss_engine.tools.registry import ToolSpec

BASELINE = Path(__file__).resolve().parents[2] / "tests" / "identity" / "undeclared_mcp_tools.txt"


def _baseline() -> list[str]:
    return sorted(line.strip() for line in BASELINE.read_text(encoding="utf-8").splitlines()
                  if line.strip() and not line.startswith("#"))


def test_every_tool_is_declared_or_on_the_shrinking_list():
    specs = build_default_registry().specs()
    undeclared = sorted(s.name for s in specs if not s.permission and not s.inherits_route_permission)
    newly_undeclared = set(undeclared) - set(_baseline())
    stale = set(_baseline()) - set(undeclared)
    assert not newly_undeclared, f"declare permission= on these new tools: {sorted(newly_undeclared)}"
    assert not stale, f"declared now; remove from {BASELINE.name}: {sorted(stale)}"


def test_every_declared_tool_names_a_catalogue_permission():
    for spec in build_default_registry().specs():
        if spec.permission:
            assert is_known(spec.permission), spec.name


def test_a_tool_with_an_unknown_permission_cannot_be_built():
    with pytest.raises(CatalogueError):
        ToolSpec("x", "x", {"type": "object"}, lambda *a, **k: {}, permission="tools:everything")
