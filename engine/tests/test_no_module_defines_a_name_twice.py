"""No module in the product defines the same top-level name twice.

Python keeps the LAST definition of a module-level name and says nothing. In builtin.py a new
helper for `draft_workflow` was written as `_declared_inputs(raw, details)` some 230 lines below
the existing `_declared_inputs(raw)`, which reads a published version's input_schema in either of
its two real shapes. The later one silently won, so `describe_workflow_run` read JSON Schema with
the drafting helper and told the operator the run was missing inputs called "required" and
"properties" — the incident the original function was written to end (21 of 45 real versions
carry JSON Schema). Nothing failed at import, and the drafting tests passed.

So this walks EVERY module of the engine, the API and the worker and refuses any top-level name
bound twice by def, class or plain assignment. A rebinding that is meant (rare) belongs inside one
definition, not as two that a reader takes for one.
"""

from __future__ import annotations

import ast
import collections
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ("engine/boss_engine", "apps/api/app", "apps/worker")


def _top_level_names(tree: ast.Module) -> list[tuple[str, int]]:
    names: list[tuple[str, int]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append((node.name, node.lineno))
        elif isinstance(node, ast.Assign):
            names.extend((t.id, node.lineno) for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            names.append((node.target.id, node.lineno))
    return names


def _modules() -> list[Path]:
    found: list[Path] = []
    for package in PACKAGES:
        found.extend(p for p in (ROOT / package).rglob("*.py") if "node_modules" not in p.parts)
    return sorted(found)


def test_the_scan_sees_the_modules_it_guards():
    """A walk that silently found nothing would pass forever; builtin.py must be in it."""
    paths = {str(p.relative_to(ROOT)) for p in _modules()}
    assert "engine/boss_engine/tools/builtin.py" in paths
    assert "apps/api/app/main.py" in paths
    assert len(paths) > 50


def test_no_module_defines_a_top_level_name_twice():
    """`_declared_inputs` was defined twice in builtin.py; the second shadowed the first and
    describe_workflow_run reported phantom inputs named 'required' and 'properties'."""
    collisions: list[str] = []
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        seen: dict[str, list[int]] = collections.defaultdict(list)
        for name, line in _top_level_names(tree):
            seen[name].append(line)
        for name, lines in seen.items():
            if len(lines) > 1:
                collisions.append(f"{path.relative_to(ROOT)}: {name} at lines {lines}")
    assert not collisions, (
        "A later definition silently replaces an earlier one with the same name. Rename one:\n  "
        + "\n  ".join(collisions)
    )


def test_the_scan_catches_a_shadowing_definition():
    """The detector itself, on the exact shape that shipped."""
    tree = ast.parse("def _declared_inputs(raw):\n    pass\n\ndef _declared_inputs(raw, details=None):\n    pass\n")
    names = [n for n, _ in _top_level_names(tree)]
    assert names.count("_declared_inputs") == 2
