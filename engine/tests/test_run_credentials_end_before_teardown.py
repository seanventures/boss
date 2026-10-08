"""id-sec-final2 residual: in both code-task services the run's credentials were revoked AFTER
sandbox.teardown() in the same `finally`, so a teardown that raised left the run's tokens, egress
credential and GitHub installation tokens alive (and the control-plane binding on a pooled thread).
Revocation now comes first, and teardown sits in an inner `finally` of it: each runs whatever the
other does."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ("engine/boss_engine/services/code_task_service.py",
            "engine/boss_engine/services/governed_code_task_service.py")


def _calls(node: ast.AST, attr: str) -> list[int]:
    return [n.lineno for n in ast.walk(node) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == attr]


def test_run_credentials_are_revoked_before_and_regardless_of_teardown():
    for rel in SERVICES:
        tree = ast.parse((ROOT / rel).read_text())
        checked = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try) or not node.finalbody:
                continue
            body = ast.Module(body=node.finalbody, type_ignores=[])
            finish, teardown = _calls(body, "finish_code_task_run"), _calls(body, "teardown")
            if not finish:
                continue
            checked += 1
            assert teardown, rel
            assert max(finish) < min(teardown), f"{rel}: revoke before teardown"
            # the revoke is a try whose finally holds the teardown
            inner = [n for n in node.finalbody if isinstance(n, ast.Try)]
            assert inner and _calls(ast.Module(body=inner[0].body, type_ignores=[]), "finish_code_task_run"), rel
            assert _calls(ast.Module(body=inner[0].finalbody, type_ignores=[]), "teardown"), rel
        assert checked == 1, rel
