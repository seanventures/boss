"""Advice the console shows names a button or a screen, never an endpoint to call.

The console walk on boss-1 (2026-10-02) found the Observability screen saying "start with POST
/v1/observability/stack/deploy" and Govern's token recommendations saying "raise that stage's
max_total_tokens_per_call via PUT /v1/token-efficiency/policies". Those strings are written by
these services and rendered as-is. The console now has the controls (Deploy monitoring stack in
Infra, Save cap in Govern), and the advice names them.

Every string literal in the services whose text reaches those panels is enumerated here (code,
not docstrings), so new advice that names an endpoint fails this test.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

SERVICES = Path(__file__).resolve().parents[1] / "boss_engine" / "services"
#: The services whose strings the Observability and Govern panels render verbatim.
RENDERED_BY_THE_CONSOLE = ("observability_suite.py", "token_efficiency.py")
ENDPOINT = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+/v1/")


def _code_strings(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                docstrings.add(id(body[0].value))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            out.append((node.lineno, node.value))
    return out


def test_console_rendered_advice_names_no_endpoint():
    offenders = []
    checked = 0
    for name in RENDERED_BY_THE_CONSOLE:
        for line, value in _code_strings(SERVICES / name):
            checked += 1
            if ENDPOINT.search(value):
                offenders.append(f"{name}:{line}: {value[:100]}")
    assert checked > 50, "the scan read too few strings to be a real check"
    assert offenders == [], "\n".join(offenders)


def test_the_advice_names_the_controls_that_exist():
    obs = (SERVICES / "observability_suite.py").read_text(encoding="utf-8")
    tok = (SERVICES / "token_efficiency.py").read_text(encoding="utf-8")
    assert "Deploy monitoring stack" in obs
    assert "Save cap" in tok


#: The keys whose text the Govern and Observability panels show as a sentence.
ADVICE_KEYS = ("action", "finding", "message")
#: A raw column or field name: snake_case with at least two underscores (max_tokens_per_day).
FIELD_NAME = re.compile(r"\b[a-z0-9]+(?:_[a-z0-9]+){2,}\b")


def _literal_text(node: ast.AST) -> str:
    """The words an operator reads: string literals and the literal parts of f-strings, not the
    names inside {...} placeholders (those are code, and are replaced by values)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values if isinstance(v, ast.Constant))
    if isinstance(node, ast.BinOp):
        return _literal_text(node.left) + _literal_text(node.right)
    if isinstance(node, ast.IfExp):
        return _literal_text(node.body) + " " + _literal_text(node.orelse)
    return ""


def test_console_rendered_advice_names_no_raw_field():
    """The review of milestone/console-b (2026-10-02) found a cost alert still telling operators to
    "adjust max_cost_usd_micros_per_day", and the endpoint scan above could not see it. Every
    action/finding/message sentence in these services is enumerated; none may name a column."""
    offenders = []
    checked = 0
    for name in RENDERED_BY_THE_CONSOLE:
        tree = ast.parse((SERVICES / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if not (isinstance(key, ast.Constant) and key.value in ADVICE_KEYS):
                    continue
                words = _literal_text(value)
                if not words.strip():
                    continue
                checked += 1
                for field in FIELD_NAME.findall(words):
                    offenders.append(f"{name}:{value.lineno}: '{field}' in {words[:90]!r}")
    assert checked >= 15, "the scan read too few advice sentences to be a real check"
    assert offenders == [], "\n".join(offenders)
