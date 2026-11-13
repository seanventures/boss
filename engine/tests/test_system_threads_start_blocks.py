"""Every system thread starts independently (regression for a merge that nested one start
inside another's except handler, so skill-source sync only ran when backups failed).

The invariant, checked on the source itself: in start_all, each try has exactly one
handler, and a handler only ever records failure (started[...] = False) — the success
mark and the start call live in the try body.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "boss_engine" / "services" / "system_threads.py"


def _started_assignments(nodes: list[ast.stmt]) -> list[tuple[int, bool]]:
    out: list[tuple[int, bool]] = []
    for node in nodes:
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Assign):
                continue
            for target in sub.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "started"
                    and isinstance(sub.value, ast.Constant)
                    and isinstance(sub.value.value, bool)
                ):
                    out.append((sub.lineno, sub.value.value))
    return out


def test_every_start_block_is_independent() -> None:
    tree = ast.parse(SRC.read_text())
    start_all = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "start_all"
    )
    tries = [n for n in ast.walk(start_all) if isinstance(n, ast.Try)]
    assert tries, "start_all lost its try blocks"
    for t in tries:
        assert len(t.handlers) == 1, (
            f"try at line {t.lineno} has {len(t.handlers)} except handlers; a second handler "
            "is unreachable and means a start block was merged into the wrong place"
        )
        for lineno, value in _started_assignments(list(t.handlers[0].body)):
            assert value is False, (
                f"line {lineno}: an except handler marks a thread as started — that start "
                "only runs when the previous block fails; move it into its own try"
            )


def test_skill_sources_started_in_a_try_body() -> None:
    tree = ast.parse(SRC.read_text())
    start_all = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "start_all"
    )
    for t in (n for n in ast.walk(start_all) if isinstance(n, ast.Try)):
        for lineno, value in _started_assignments(t.body):
            if value is True:
                src_line = SRC.read_text().splitlines()[lineno - 1]
                if "skill_sources" in src_line:
                    return
    raise AssertionError("skill_sources is never marked started inside a try body")
