"""No module defines a top-level function or class twice.

A second `def` of the same name silently replaces the first for every caller. That is how the
app-platform merge shipped two `preview_service.preview_port()`s: F5's "a free port from
BOSS_PREVIEW_PORTS" replaced the preview ORIGIN's port (BOSS_PREVIEW_PORT, 3001), so the preview
listener and every preview URL took a random app port. Python raises nothing; this does.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TREES = ("engine/boss_engine", "apps/api/app", "apps/worker", "apps/egress")


def test_no_top_level_name_is_defined_twice():
    twice = []
    for tree in TREES:
        for path in sorted((ROOT / tree).rglob("*.py")):
            seen: dict[str, int] = {}
            for node in ast.parse(path.read_text(encoding="utf-8")).body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name in seen:
                        twice.append(f"{path.relative_to(ROOT)}: {node.name} (lines {seen[node.name]} and {node.lineno})")
                    seen[node.name] = node.lineno
    assert not twice, twice


def test_the_preview_origin_port_and_the_app_port_are_two_functions(monkeypatch):
    from boss_engine.services import preview_service as ps

    monkeypatch.delenv("BOSS_PREVIEW_PORT", raising=False)
    assert ps.preview_port() == 3001
    assert 19000 <= ps.app_preview_port() <= 19999
