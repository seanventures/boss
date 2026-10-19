"""A function's local never reuses a name the module imported.

governed_code_task_service imports `_release` (the budget's release) and every exit of
_run_governed_code_task calls it; 049c97709 assigned `_release = release_scope.of_run(...)` in the
same function, so every release-scoped fix build died at its end with "'dict' object is not
callable" (Order API release 1.2's incident fix, 2026-10-19). Checked by AST for the build
pipeline's modules: a local assignment to an imported name is refused.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SERVICES = Path(__file__).resolve().parents[1] / "boss_engine" / "services"
MODULES = ["governed_code_task_service.py", "code_task_service.py",
           "workflow_capability_runner.py", "incident_response.py", "app_alerts.py"]


def _imported(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom | ast.Import):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
    return names


@pytest.mark.parametrize("module", MODULES)
def test_no_function_assigns_a_name_its_module_imported(module):
    tree = ast.parse((SERVICES / module).read_text())
    imported = _imported(tree)
    bad = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(fn):
            targets = (node.targets if isinstance(node, ast.Assign) else
                       [node.target] if isinstance(node, ast.AnnAssign | ast.AugAssign) else [])
            for t in targets:
                if isinstance(t, ast.Name) and t.id in imported and t.id.startswith("_"):
                    bad.append(f"{fn.name}:{node.lineno} assigns {t.id}")
    assert not bad, bad
