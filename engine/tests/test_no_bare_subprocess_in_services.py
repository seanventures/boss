"""Every process BOSS's services start is owned (stability spec §4a.2, enforcing test).

A bare `subprocess.Popen`/`run` kills ONE pid on timeout and nothing on exit, which is how a
killed agent left its dev server, watcher and Chromium running, and then 9 zombies. The fix
is one spawn helper, `owned_process`, that ends the whole tree every time — and this test
keeps it the only way in: a new bare call anywhere in `boss_engine/services` fails here, by
file and line, with what to use instead.
"""
from __future__ import annotations

import ast
from pathlib import Path

SERVICES = Path(__file__).resolve().parents[1] / "boss_engine" / "services"

#: The one place the stock calls may appear: the helper itself (and its `ps` census).
ALLOWED = {"owned_process.py"}

_SPAWNERS = {"Popen", "run", "call", "check_call", "check_output", "getoutput",
             "getstatusoutput"}


def _bare_calls(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = {"subprocess"}
    direct: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "subprocess":
                    aliases.add(a.asname or "subprocess")
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for a in node.names:
                if a.name in _SPAWNERS:
                    direct.add(a.asname or a.name)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                and f.value.id in aliases and f.attr in _SPAWNERS):
            found.append(f"{path.name}:{node.lineno} subprocess.{f.attr}(")
        elif isinstance(f, ast.Name) and f.id in direct:
            found.append(f"{path.name}:{node.lineno} {f.id}(")
    return found


def test_no_service_spawns_a_process_it_does_not_own():
    offenders = []
    for path in sorted(SERVICES.rglob("*.py")):
        if path.name in ALLOWED:
            continue
        offenders.extend(_bare_calls(path))
    assert not offenders, (
        "Spawn through boss_engine.services.owned_process (run / popen), never subprocess "
        "directly: it puts the child in its own group, tags every descendant, and ends the "
        "whole tree on exit, timeout and cancel.\n  " + "\n  ".join(offenders))


def test_the_lint_sees_every_spelling(tmp_path):
    sample = tmp_path / "x.py"
    sample.write_text(
        "import subprocess\nimport subprocess as sp\nfrom subprocess import Popen as P\n"
        "subprocess.run(['a'])\nsp.check_output(['b'])\nP(['c'])\n", encoding="utf-8")
    assert len(_bare_calls(sample)) == 3
