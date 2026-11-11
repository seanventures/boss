"""One structured shape for the evidence a failed attempt hands the next one (BOSSAPP-960).

Every repair path fed its successor PROSE: the in-run repair loop sent the last 3000 characters
of test output, a cold retry sent the last 60 lines of `last_verify_output`, the shepherd sent a
one-line note, and a CI failure sent nothing at all. The one channel that worked — review
comments — is exactly the one that is structured (`review_comments.ReviewComment`: severity,
path, line, quoted source). A model handed "the tail of stdout" re-reads the whole failure every
attempt and routinely fixes the wrong thing; handed *which test failed, on which assertion, in
which file, with the line quoted*, it goes straight there.

`RepairFinding` is that shape for everything that is not a review comment: a failing test (with
its assertion), a compiler error, a failing CI check. `find()` parses the common toolchains'
output — pytest, jest/vitest, tsc, go test — and `render()` writes the findings in the same
`FINDING: <SEVERITY> path:line — text` grammar the review channel already uses, so one grammar
reaches the implementer whichever path the evidence travelled. Parsing is best-effort by design:
`evidence_block()` falls back to the raw tail when nothing parses, because evidence that is
merely unstructured must still arrive.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

#: Findings per block: enough to act on, few enough to stay inside a prompt's budget.
MAX_FINDINGS = 20
#: The raw tail kept under the structured findings — context for what the parser missed.
RAW_TAIL_CHARS = 800
#: The legacy tail, kept whole when nothing parses.
FALLBACK_TAIL_CHARS = 3000


@dataclass(frozen=True)
class RepairFinding:
    """One concrete thing the next attempt must fix."""

    source: str                       # tests | ci | shepherd | install | review
    text: str                         # one line: what is wrong
    severity: str = "MAJOR"           # the review channel's vocabulary (review_comments)
    path: Optional[str] = None
    line: Optional[int] = None
    quote: Optional[str] = None       # the output line this finding was read from, verbatim
    failing_test: Optional[str] = None
    assertion: Optional[str] = None


# ── Parsers ───────────────────────────────────────────────────────────────────────────────────

#: pytest's short summary: `FAILED tests/test_x.py::test_y - AssertionError: 1 != 2`.
_PYTEST_FAILED = re.compile(r"^(FAILED|ERROR) ([^\s:]+?\.py)(::\S+)?(?: - (.+))?$", re.MULTILINE)
#: pytest's long form assertion lines: `E   assert resp.status == 200`.
_PYTEST_E = re.compile(r"^E {3}(.+)$", re.MULTILINE)
#: tsc: `src/App.tsx(12,5): error TS2304: Cannot find name 'x'.`
_TSC_PAREN = re.compile(r"^(\S+?)\((\d+),\d+\): (error TS\d+: .+)$", re.MULTILINE)
#: tsc --pretty / esbuild / many linters: `src/App.tsx:12:5 - error TS2304: ...`
_TSC_COLON = re.compile(r"^(\S+?):(\d+):\d+ ?[-—] ?(error .+)$", re.MULTILINE)
#: jest / vitest failing test headers: `✕ renders the form (23 ms)` or `× renders the form`.
_JEST_FAIL = re.compile(r"^\s*[✕×✗] (.+?)(?: \(\d+ ?ms\))?$", re.MULTILINE)
#: jest / vitest `● suite › name` blocks (they precede the expect() detail).
_JEST_DOT = re.compile(r"^\s*[●❯] (.+)$", re.MULTILINE)
#: a jest/vitest/node assertion detail line.
_EXPECT = re.compile(r"^\s*((?:expect\(|AssertionError[:\s]).+)$", re.MULTILINE)
#: go test: `--- FAIL: TestName (0.00s)`.
_GO_FAIL = re.compile(r"^--- FAIL: (\S+)", re.MULTILINE)


def _pytest(output: str) -> list[RepairFinding]:
    found: list[RepairFinding] = []
    assertions = [m.group(1).strip() for m in _PYTEST_E.finditer(output)]
    for m in _PYTEST_FAILED.finditer(output):
        kind, path, test, said = m.group(1), m.group(2), m.group(3) or "", m.group(4)
        assertion = (said or "").strip() or (assertions[0] if len(assertions) == 1 else None)
        found.append(RepairFinding(
            source="tests", severity="MAJOR" if kind == "FAILED" else "BLOCKER",
            text=f"{kind.lower()}: {path}{test}" + (f" — {assertion}" if assertion else ""),
            path=path, failing_test=f"{path}{test}", assertion=assertion,
            quote=m.group(0).strip()))
    return found


def _tsc(output: str) -> list[RepairFinding]:
    found: list[RepairFinding] = []
    for rx in (_TSC_PAREN, _TSC_COLON):
        for m in rx.finditer(output):
            found.append(RepairFinding(
                source="tests", severity="BLOCKER", text=m.group(3).strip(),
                path=m.group(1), line=int(m.group(2)), quote=m.group(0).strip()))
    return found


def _jest(output: str) -> list[RepairFinding]:
    names = [m.group(1).strip() for m in _JEST_FAIL.finditer(output)] or \
            [m.group(1).strip() for m in _JEST_DOT.finditer(output)]
    details = [m.group(1).strip() for m in _EXPECT.finditer(output)]
    found: list[RepairFinding] = []
    for i, name in enumerate(dict.fromkeys(names)):
        assertion = details[i] if i < len(details) else (
            details[0] if len(details) == 1 else None)
        found.append(RepairFinding(
            source="tests", text=f"failing test: {name}" + (f" — {assertion}" if assertion else ""),
            failing_test=name, assertion=assertion, quote=name))
    return found


def _go(output: str) -> list[RepairFinding]:
    return [RepairFinding(source="tests", text=f"failing test: {m.group(1)}",
                          failing_test=m.group(1), quote=m.group(0).strip())
            for m in _GO_FAIL.finditer(output)]


def find(output: str) -> list[RepairFinding]:
    """The structured findings in a verify/test run's output; [] when nothing parses.

    pytest first (its FAILED lines carry the most), then compilers, then jest-family, then go;
    deduplicated on (failing_test, path, line, text) and capped at MAX_FINDINGS.
    """
    text = str(output or "")
    if not text.strip():
        return []
    found = _pytest(text) + _tsc(text) + _jest(text) + _go(text)
    unique: dict = {}
    for f in found:
        unique.setdefault((f.failing_test, f.path, f.line, f.text), f)
    return list(unique.values())[:MAX_FINDINGS]


def from_ci(failing: Iterable[Any], summary: str = "", url: Optional[str] = None
            ) -> list[RepairFinding]:
    """Findings from a CI failure: one per failing check. CI gives check NAMES, not logs
    (ci_gate reads the checks API), so the finding says which check and where to read it."""
    found = [RepairFinding(
        source="ci", severity="BLOCKER", text=f"CI check failed: {str(name)[:200]}",
        failing_test=str(name)[:200], quote=(summary or "").strip()[:300] or None)
        for name in failing if str(name or "").strip()]
    if not found and (summary or "").strip():
        found = [RepairFinding(source="ci", severity="BLOCKER",
                               text=(summary or "").strip()[:300])]
    if url and found:
        found.append(RepairFinding(source="ci", severity="MINOR",
                                   text=f"full logs: {url}"))
    return found[:MAX_FINDINGS]


def render(findings: Iterable[RepairFinding], budget: int = 4000) -> str:
    """The findings in the review channel's grammar, one per line, inside `budget` characters.

    `FINDING: <SEVERITY> [path:line —] text`, then indented `test:` / `assertion:` / `quote:`
    lines where known — the same first line `review_comments.parse_findings` reads, so every
    repair path now speaks the one grammar the implementer prompt already explains."""
    lines: list[str] = []
    spent = 0
    for f in findings:
        where = f"{f.path}:{f.line} — " if f.path and f.line else (f"{f.path} — " if f.path else "")
        block = [f"FINDING: {f.severity} {where}{f.text}".rstrip()]
        if f.failing_test and f.failing_test not in block[0]:
            block.append(f"    test: {f.failing_test}")
        if f.assertion and f.assertion not in block[0]:
            block.append(f"    assertion: {f.assertion}")
        if f.quote and f.quote != block[0] and f.quote not in block[0]:
            block.append(f"    quote: {f.quote[:300]}")
        text = "\n".join(block)
        if spent + len(text) + 1 > budget:
            break
        lines.append(text)
        spent += len(text) + 1
    return "\n".join(lines)


def evidence_block(output: str, *, command: str = "", exit_code: Optional[int] = None,
                   source: str = "tests") -> str:
    """What a repair attempt is told about a failed run: structured findings when they parse,
    the raw tail either way (short under findings, whole — the legacy 3000 — without them).

    This is the drop-in replacement for `f"$ {cmd} (exit {code})\\n{output[-3000:]}"`: the
    header line is kept so nothing downstream loses the command or the exit code."""
    header = f"$ {command} (exit {exit_code})" if command else ""
    raw = str(output or "")
    findings = find(raw) if source == "tests" else []
    if not findings:
        tail = raw[-FALLBACK_TAIL_CHARS:]
        return "\n".join(p for p in (header, tail) if p)
    parts = [header] if header else []
    parts.append(f"STRUCTURED FINDINGS ({len(findings)} — fix these, each where it points):")
    parts.append(render(findings))
    tail = raw[-RAW_TAIL_CHARS:].strip()
    if tail:
        parts.append(f"(end of raw output, for context)\n{tail}")
    return "\n".join(parts)
