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

#: A test function's name: Go/JUnit `TestX`/`testX`, Python `test_x`.
_NAME = r"(Test[A-Z0-9_]\w{2,}|test_[a-z0-9_]{3,}|test[A-Z]\w{2,})"
#: Where a brief NAMES a test (#174 review): quoted at the start of a backticked span
#: (`TestX`, `TestX(t *testing.T)`), or as the item of an explicit list ("- TestX — …").
_QUOTED = re.compile(r"`" + _NAME + r"(?:\s*\([^`]*\))?`")
_LISTED = re.compile(r"(?m)^\s*(?:[-*]|\d+[.)])\s+(?:Added:\s*)?" + _NAME + r"\b")
#: The last word of a name that is a fixture or a helper, never a test (`test_client`,
#: `TestHelpers`, `testdata`).
_FIXTURE_WORDS = frozenset({
    "helper", "helpers", "client", "clients", "fixture", "fixtures", "util", "utils", "data",
    "config", "setup", "teardown", "main", "case", "cases", "suite", "server", "app", "db",
    "env", "mode", "harness", "support", "common", "mock", "mocks", "stub", "stubs"})

#: Where a test file DEFINES a test, per language.
_DEFINES = (
    re.compile(r"(?m)^\s*func\s+(Test\w+)\s*\("),                         # Go
    re.compile(r"(?m)^\s*(?:async\s+)?def\s+(test_\w+|test[A-Z]\w*)\s*\("),  # Python
    re.compile(r"(?m)\b(?:void|fun|suspend\s+fun)\s+(test\w*|`[^`]+`)\s*\("),  # Java/Kotlin
    re.compile(r"(?m)\b(?:it|test)\s*\(\s*['\"`]([^'\"`\n]+)['\"`]"),       # JS/TS titles
)

#: A suite run whose output lists each test by name (go test -v, pytest -v, JUnit reports).
_LISTS_TESTS = re.compile(r"(?m)^(?:=== RUN|--- (?:PASS|FAIL|SKIP)|\S+::\S+ (?:PASSED|FAILED|SKIPPED)|"
                          r"\[INFO\] Tests run:.*in )")


def _fixture_like(name: str) -> bool:
    words = re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", name.replace("_", " "))
    rest = [w.lower() for w in words if w.lower() != "test"]
    # A fixture is a short noun (`test_client`, `TestHelpers`, `test_app_config`); a test
    # names a behaviour (`TestP1_ReadyzFailsOnBadConfig` is a test, whatever its last word).
    return (not rest or name.lower() == "testdata"
            or (len(rest) <= 2 and rest[-1] in _FIXTURE_WORDS))


def named(*texts: Optional[str]) -> list[str]:
    """The test names the brief (and its plan) call for, in order, without repeats: only names
    QUOTED as code or LISTED as items, never a word of prose, and never a fixture or helper."""
    out: list[str] = []
    for text in texts:
        text = str(text or "")
        found = sorted([(m.start(), m.group(1)) for m in _QUOTED.finditer(text)]
                       + [(m.start(), m.group(1)) for m in _LISTED.finditer(text)])
        for _, name in found:
            if not _fixture_like(name) and name not in out:
                out.append(name)
    return out


def defined(root: Path | str, test_paths: Iterable[str]) -> set[str]:
    """The test names the given test files (relative to `root`) define WITH A BODY: a Go test
    with no statement, or a Python one that is only `pass`/`...`, is not written (#174 review)."""
    base = Path(root)
    out: set[str] = set()
    for rel in test_paths or ():
        try:
            text = (base / rel).read_text(errors="ignore")
        except OSError:
            continue
        for rx in _DEFINES:
            for m in rx.finditer(text):
                if not _empty_body(text, m.end(), rel):
                    out.add(m.group(1).strip("`"))
    return out


def _empty_body(text: str, after: int, rel: str) -> bool:
    """Whether the test defined just before `after` has no statement (cheap, per language)."""
    if rel.endswith(".go"):
        open_at = text.find("{", after)
        if open_at < 0:
            return False
        depth, i = 0, open_at
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        body = re.sub(r"//[^\n]*|/\*.*?\*/", "", text[open_at + 1:i], flags=re.S)
        return not body.strip()
    if rel.endswith(".py"):
        colon = text.find(":", text.find(")", after))
        nxt = re.search(r"(?m)^(?=\S)|^\s*(?:async\s+)?def\s|^\s*@", text[colon + 1:])
        body = text[colon + 1: colon + 1 + nxt.start()] if nxt else text[colon + 1:]
        lines = [ln.strip() for ln in body.splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        lines = [ln for ln in lines if not re.fullmatch(r'(?:"""|\'\'\').*', ln)]
        return all(ln in ("pass", "...") for ln in lines)
    return False


_SKIPPED_LINE = re.compile(r"--- SKIP\b|\bSKIPPED\b|\bskipped\b")


def ran(name: str, verify_output: str) -> Optional[bool]:
    """Whether a suite run listed `name` as RUN — a `--- SKIP` or pytest SKIPPED line is not a
    run (#174 review); None when its output lists no test by name (then the suite passing is
    all there is to read)."""
    out = str(verify_output or "")
    if not _LISTS_TESTS.search(out):
        return None
    hit = re.compile(r"(?<![\w])" + re.escape(name) + r"(?![\w])")
    lines = [ln for ln in out.splitlines() if hit.search(ln)]
    return any(not _SKIPPED_LINE.search(ln) and not ln.startswith("=== RUN") for ln in lines)


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
