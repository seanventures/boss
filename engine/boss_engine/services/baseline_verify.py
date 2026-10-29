"""Baseline-aware verification: classify failing tests as caused, pre_existing,
or environment relative to the build's base commit, so repair never chases a
failure the change did not introduce.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence

_FAILED_LINE_RE = re.compile(r"^(?:FAILED|ERROR)\s+(?P<test_id>\S+)\s*(?:[-\[].*)?$", re.MULTILINE)

# pytest skip summary: "SKIPPED tests/x.py::test_a" or "SKIPPED [1] tests/x.py:12: reason"
_SKIPPED_LINE_RE = re.compile(
    r"^SKIPPED\s+(?:\[\d+\]\s+)?(?P<test_id>\S+?):?\s*(?:[-\[].*)?$", re.MULTILINE
)

# vitest/jest: "FAIL path/to/file.test.ts > suite name > test name"
_JS_FAIL_LINE_RE = re.compile(r"^(?:FAIL|✗|✕)\s+(?P<test_id>\S.*\S)\s*$", re.MULTILINE)

# the pytest FAILURES-section divider that precedes a traceback: "____ test_name ____"
_SECTION_HEADER_RE = re.compile(r"^_{3,}\s*(?P<name>.+?)\s*_{3,}$", re.MULTILINE)

_ENVIRONMENT_SIGNAL_RE = re.compile(
    r"ImportError|ModuleNotFoundError|PermissionError|SyntaxError|"
    r"ERROR collecting|no tests ran"
)


@dataclass(frozen=True)
class Classified:
    test_id: str
    klass: str  # "caused" | "pre_existing" | "environment"
    reason: str
    evidence: str


@dataclass(frozen=True)
class BaselineRecord:
    caused: tuple = field(default_factory=tuple)
    pre_existing: tuple = field(default_factory=tuple)
    environment: tuple = field(default_factory=tuple)

    def as_dict(self) -> dict:
        def _enc(items):
            return [
                {"test_id": c.test_id, "klass": c.klass, "reason": c.reason, "evidence": c.evidence}
                for c in items
            ]

        return {
            "caused": _enc(self.caused),
            "pre_existing": _enc(self.pre_existing),
            "environment": _enc(self.environment),
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "BaselineRecord":
        if not data:
            return cls()

        def _dec(items):
            return tuple(
                Classified(
                    test_id=i["test_id"],
                    klass=i["klass"],
                    reason=i.get("reason", ""),
                    evidence=i.get("evidence", ""),
                )
                for i in items or []
            )

        return cls(
            caused=_dec(data.get("caused")),
            pre_existing=_dec(data.get("pre_existing")),
            environment=_dec(data.get("environment")),
        )


def extract_test_ids(output: str) -> list:
    text = output or ""
    ids = [m.group("test_id") for m in _FAILED_LINE_RE.finditer(text)]
    ids += [m.group("test_id") for m in _SKIPPED_LINE_RE.finditer(text)]
    ids += [m.group("test_id") for m in _JS_FAIL_LINE_RE.finditer(text)]
    return list(dict.fromkeys(ids))


def _slice_for(test_id: str, output: str) -> str:
    """The test's own failure block. Pytest's FAILURES section prints the traceback under a
    "____ test_name ____" divider well BEFORE the short-summary "FAILED id - reason" line, so
    the block starts at that divider (matched against the test's own short name) when present,
    and always includes the short-summary/SKIPPED/FAIL header line itself at the end."""
    lines = (output or "").splitlines()
    short_name = test_id.rsplit("::", 1)[-1].split("[", 1)[0]

    summary_idx = [
        i for i, line in enumerate(lines)
        if (_FAILED_LINE_RE.match(line) or _SKIPPED_LINE_RE.match(line) or _JS_FAIL_LINE_RE.match(line))
        and test_id in line
    ]
    section_idx = [
        i for i, line in enumerate(lines)
        if (m := _SECTION_HEADER_RE.match(line)) and short_name and short_name in m.group("name")
    ]
    all_headers = [
        i for i, line in enumerate(lines)
        if _FAILED_LINE_RE.match(line) or _SKIPPED_LINE_RE.match(line)
        or _JS_FAIL_LINE_RE.match(line) or _SECTION_HEADER_RE.match(line)
    ]

    start = min(section_idx + summary_idx) if (section_idx or summary_idx) else None
    if start is None:
        matched = [line for line in lines if test_id in line]
        return "\n".join(matched) if matched else (output or "")

    later_headers = [i for i in all_headers if i > start]
    end = later_headers[0] if later_headers else len(lines)
    block = lines[start:end]
    if not any(i in summary_idx for i in range(start, end)):
        block += [lines[i] for i in summary_idx]
    return "\n".join(block)


def _environment_signal(slice_text: str) -> Optional[str]:
    match = _ENVIRONMENT_SIGNAL_RE.search(slice_text or "")
    return match.group(0) if match else None


def classify(
    failing_ids,
    head_output: str,
    base_outcomes: Mapping[str, str],
    *,
    classify_environment: Callable[[str, str, Optional[str]], tuple],
) -> BaselineRecord:
    caused = []
    pre_existing = []
    environment = []

    for test_id in sorted(failing_ids):
        slice_text = _slice_for(test_id, head_output)
        base_outcome = base_outcomes.get(test_id)
        if base_outcome in ("failed", "error"):
            pre_existing.append(
                Classified(test_id=test_id, klass="pre_existing", reason="fails on base too", evidence=slice_text)
            )
            continue

        signal = _environment_signal(slice_text)
        klass, reason = classify_environment(test_id, slice_text, signal)
        if klass == "environment":
            environment.append(Classified(test_id=test_id, klass="environment", reason=reason, evidence=slice_text))
        else:
            caused.append(Classified(test_id=test_id, klass="caused", reason=reason, evidence=slice_text))

    return BaselineRecord(caused=tuple(caused), pre_existing=tuple(pre_existing), environment=tuple(environment))


def pr_section(record: BaselineRecord) -> str:
    if not record.pre_existing and not record.environment:
        return ""

    lines = ["## Not caused by this change", ""]
    if record.pre_existing:
        lines.append("Pre-existing failures (fail on the base commit too):")
        for c in record.pre_existing:
            lines.append(f"- `{c.test_id}` — {c.reason}")
        lines.append("")
    if record.environment:
        lines.append("Environment failures (error before any assertion on the change):")
        for c in record.environment:
            lines.append(f"- `{c.test_id}` — {c.reason}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def base_outcomes_from_output(ids, base_output: str) -> dict:
    """Outcome ("failed"/"passed") for each id, read off a test run of the SAME ids against the
    base commit: whatever still shows up in the base run's failure summary failed there too."""
    present = set(extract_test_ids(base_output))
    if not _base_run_completed(base_output):
        # NO EVIDENCE IS NOT A PASS. An empty or unusable base run (the worktree could not be
        # made, the runner refused, nothing was collected) said "passed" for every id, and that
        # was cached per base SHA, so every later build labelled every failure "caused"
        # (boss-1, 2026-10-29: 69 cached rows, all "passed").
        return {test_id: ("failed" if test_id in present else "unknown") for test_id in ids}
    return {test_id: ("failed" if test_id in present else "passed") for test_id in ids}


#: A test run's own closing line: pytest's "N passed/failed/error(s) in Xs", "no tests ran".
_RUN_SUMMARY = re.compile(r"(?m)^\W*(?:\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed)\b"
                          r"|no tests ran\b|Tests\s+\d+)")


def _base_run_completed(base_output: str) -> bool:
    """Did the base rerun actually run tests (its summary line is there)?"""
    return bool(_RUN_SUMMARY.search(base_output or ""))


def default_classify_environment(test_id: str, slice_text: str, signal: Optional[str]) -> tuple:
    """The production classify_environment: an environment signal (import/module/permission
    error, a collection failure) before any assertion ran means the change did not cause it."""
    if signal:
        return ("environment", f"{signal} before any assertion on the change")
    return ("caused", "new failure introduced by this change")


def cleanup_findings(record: BaselineRecord) -> list:
    return [
        {"severity": "low", "text": f"{c.test_id}: {c.reason}"}
        for c in record.pre_existing
    ]


def environment_signature(classified: Classified) -> str:
    """A stable key for an `environment`-classified failure, so the SAME underlying infra
    problem (a missing driver, a collection error) is recognised as recurring across different
    runs and test ids rather than counted as a fresh one each time."""
    signal = (_environment_signal(classified.evidence) or classified.reason or "").strip()
    return signal or "unknown"
