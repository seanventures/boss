"""RED FIRST: a fix's regression test must fail on the faulty code before the fix exists.

Incident Response asks for it in so many words (workflow_capability_runner.INCIDENT_FIX_CRITERIA:
"A regression test reproduces the incident's failing requests and FAILS on the faulty release;
it passes with the fix"). Nothing checked the first half: the build recorded only that the
suite passed WITH the fix, so a test that would have passed on the broken release too — one
that never reproduced the incident — read exactly like a real regression test.

The governed build proves it at the one moment it is cheap and exact: QA has written the tests
and the implementer has not yet touched the code, so the tree IS the faulty release plus the
new tests. BOSS runs the app's own test command there (the same one every gate runs) and records
red (it failed, and which tests) or not red. With the fix the same suite is the build's green.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

#: A criterion asks for red first when it says the test fails on the faulty / current / broken
#: code (or release, or version) — the words an incident fix or a bug fix is briefed with.
_DEMAND = re.compile(
    r"\bfails?\b[^.]{0,40}\b(on|against|with)\s+the\s+(faulty|broken|current|buggy|"
    r"unfixed|bad|failing)\b|\bfails?\s+before\s+the\s+fix\b", re.I)

#: unittest / pytest / jest failure lines: "FAIL: test_x (pkg.tests.test_mod.Case)",
#: "FAILED tests/test_mod.py::test_x", "ERROR: test_x (...)", "✕ name", "● Suite › name".
_FAIL_LINE = re.compile(r"^(?:FAIL|ERROR|FAILED)[: ]\s*(.+)$|^\s*(?:✕|●)\s+(.+)$")


def demanded(criteria: Iterable[Any]) -> bool:
    return any(_DEMAND.search(str(c or "")) for c in criteria or ())


def failing_tests(output: str, test_files: Iterable[str]) -> list[str]:
    """The failing test lines that belong to one of `test_files` (by module or file name)."""
    stems = {re.sub(r"\.(py|[jt]sx?|mjs|cjs)$", "", str(p).rsplit("/", 1)[-1])
             for p in test_files or ()}
    stems = {s.replace(".test", "").replace(".spec", "") for s in stems} | stems
    out: list[str] = []
    for line in (output or "").splitlines():
        m = _FAIL_LINE.match(line.strip())
        if not m:
            continue
        name = (m.group(1) or m.group(2) or "").strip()
        if any(s and s in name for s in stems) and name not in out:
            out.append(name[:200])
    return out[:10]


def record(result: Any, test_files: Iterable[str]) -> dict[str, Any]:
    """What the run on the faulty code showed, for the build's state (`red_first`)."""
    files = [str(f) for f in test_files or ()]
    output = f"{getattr(result, 'stdout', '') or ''}\n{getattr(result, 'stderr', '') or ''}"
    exit_code = getattr(result, "exit_code", None)
    timed_out = bool(getattr(result, "timed_out", False))
    failed = (exit_code not in (0, None)) and not timed_out
    failing = failing_tests(output, files) if failed else []
    if timed_out:
        word = "The test run on the faulty code timed out; red first is not proven"
    elif failed and failing:
        word = f"Fails on the faulty code: {len(failing)} new test(s) red"
    elif failed:
        word = "The suite fails on the faulty code (no new test named in the output)"
    else:
        word = "Passes on the faulty code: the new tests do not reproduce the failure"
    return {"ran": True, "failed": bool(failed),
            "reproduces": bool(failing), "exit_code": exit_code, "timed_out": timed_out,
            "failing": failing, "files": files, "word": word,
            "tail": output.strip()[-1500:]}
