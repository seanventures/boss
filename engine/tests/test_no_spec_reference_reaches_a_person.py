"""No spec reference reaches a person.

The tester found "(D3)" and "(§2.4)" in what the app platform says to people: an import refused
because "the person who imported a template cannot approve it (D3)", a live key's decision "(§2.4)".
Those are the build docs' section names; nobody reading a refusal or a decision can use them.

Every string the templates, security-scan and app-links code can SAY (any string constant that is
not a docstring) is checked: no `(D3)`, `§2.4`, `(S2.5)`, `(F2 …)`. Comments and docstrings may
cite the spec; messages may not.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC_REF = re.compile(r"§\s*\d|\((?:D\d+|S\d+\.\d+|F\d+(?:\s[^)]*)?)\)")
PLACES = [
    ROOT / "engine" / "boss_engine" / "templates",
    ROOT / "engine" / "boss_engine" / "security_scan",
    ROOT / "apps" / "api" / "app" / "routes_templates.py",
    ROOT / "apps" / "api" / "app" / "routes_security.py",
]
#: module constants that NAME the spec for maintainers (never sent): allowed by name
MAINTAINER_NAMES = {"SPEC"}


def _docstrings(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
        # bare string statements (section comments written as strings) are not said either
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            ids.add(id(node.value))
    return ids


def _maintainer_constants(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) in MAINTAINER_NAMES for t in node.targets):
            for sub in ast.walk(node.value):
                ids.add(id(sub))
    return ids


def _files():
    for place in PLACES:
        yield from ([place] if place.is_file() else sorted(place.rglob("*.py")))


def test_no_message_carries_a_spec_reference():
    offenders: list[str] = []
    checked = 0
    for path in _files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _docstrings(tree) | _maintainer_constants(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
                checked += 1
                if SPEC_REF.search(node.value):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: {node.value[:80]!r}")
    assert checked > 200, "the scan found almost no strings: the places moved"
    assert not offenders, "spec references in what BOSS says to people:\n" + "\n".join(offenders)
