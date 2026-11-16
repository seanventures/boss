"""The tests a brief NAMES, and whether the tree has them and the suite ran them (BOSSAPP-1330).

A checks-failing follow-up for FXQuote (crun_5daadf83) was planned around five named tests
(`TestP1_QuoteIDsAreNotPredictable`, `…_InvalidSpread`, `…EvictsExpiredEntries`,
`…SingleRuntimeDependency`, `…QuoteIDFailureIsRejectedAndMetered`). The test author was told to
close an EMPTY gap ("Tests already pass and no gap was specified. Nothing to change."), the
implementer may not edit the suite and reverted the tests it had written, and every gate passed:
`coder_did_not_edit_tests` says the coder kept off the oracle, not that the oracle has what the
brief asked for. Acceptance fell from 17 to 13 met.

So a brief that names tests is held to them: `named` reads the test names a brief and its plan
call for (the conventions of Go, Python, Java/Kotlin and JS/TS test functions), `defined` reads
which of them the tree's test files define, and `ran` whether a suite whose output lists the
tests it ran listed them. The gate `brief_tests_written` fails while one is missing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional

#: A test function's name in a brief: Go/JUnit `TestX`/`testX`, Python `test_x`.
_NAMED = re.compile(r"(?<![\w.])(Test[A-Z0-9_]\w{2,}|test_[a-z0-9_]{3,}|test[A-Z]\w{2,})(?![\w])")
#: Names that are conventions or helpers, not a test the brief asks for.
_NOT_A_TEST = frozenset({"TestMain", "TestCase", "TestClient", "Testing", "testing"})

#: Where a test file DEFINES a test, per language.
_DEFINES = (
    re.compile(r"(?m)^\s*func\s+(Test\w+)\s*\("),                         # Go
    re.compile(r"(?m)^\s*(?:async\s+)?def\s+(test_\w+|test[A-Z]\w*)\s*\("),  # Python
    re.compile(r"(?m)\b(?:void|fun|suspend\s+fun)\s+(test\w*|`[^`]+`)\s*\("),  # Java/Kotlin
    re.compile(r"(?m)\b(?:it|test)\s*\(\s*['\"`]([^'\"`\n]+)['\"`]"),       # JS/TS titles
)

#: A suite run whose output lists each test by name (go test -v, pytest -v, JUnit reports).
_LISTS_TESTS = re.compile(r"(?m)^(?:=== RUN|--- (?:PASS|FAIL)|\S+::\S+ (?:PASSED|FAILED)|"
                          r"\[INFO\] Tests run:.*in )")


def named(*texts: Optional[str]) -> list[str]:
    """The test names the brief (and its plan) call for, in order, without repeats."""
    out: list[str] = []
    for text in texts:
        for m in _NAMED.finditer(str(text or "")):
            name = m.group(1)
            if name not in _NOT_A_TEST and name not in out:
                out.append(name)
    return out


def defined(root: Path | str, test_paths: Iterable[str]) -> set[str]:
    """The test names the given test files (relative to `root`) define."""
    base = Path(root)
    out: set[str] = set()
    for rel in test_paths or ():
        try:
            text = (base / rel).read_text(errors="ignore")
        except OSError:
            continue
        for rx in _DEFINES:
            out |= {m.group(1).strip("`") for m in rx.finditer(text)}
    return out


def ran(name: str, verify_output: str) -> Optional[bool]:
    """Whether a suite run listed `name`; None when its output lists no test by name (then the
    suite passing is all there is to read)."""
    out = str(verify_output or "")
    if not _LISTS_TESTS.search(out):
        return None
    return bool(re.search(r"(?<![\w])" + re.escape(name) + r"(?![\w])", out))


def missing(names: Iterable[str], root: Path | str, test_paths: Iterable[str],
            verify_output: str = "") -> list[str]:
    """The named tests the tree does not define, or that a suite listing its tests did not run.
    A name of a pre-existing test the brief only mentions is defined, so it never counts."""
    have = defined(root, test_paths)
    out = []
    for name in names or ():
        if name not in have or ran(name, verify_output) is False:
            out.append(name)
    return out


def brief_note(names: Iterable[str]) -> str:
    """The write-tests prompt's list of the tests the brief names."""
    names = list(names or ())
    if not names:
        return ""
    return ("\n\nTHE BRIEF NAMES THESE TESTS — each must exist in the suite under exactly this "
            "name and run green (a test that already exists stays as it is):\n"
            + "\n".join(f"  - {n}" for n in names[:40]) + "\n")


__all__ = ["named", "defined", "ran", "missing", "brief_note"]
