#!/usr/bin/env python3
"""RED IS NOT NORMAL (BOSSAPP-1429): one known-failures baseline per suite, and it only shrinks.

Every PR used to report "19-23 failures, same as main", and a security guard stayed red for weeks
because its red was accepted. The baselines under tests/known-failures/ name each failure that
happens on main today, with its ticket, an owner, the date it was listed and why it fails:

    tests/known-failures/engine.txt    engine/tests/...
    tests/known-failures/api.txt       apps/api/tests/...
    tests/known-failures/worker.txt    apps/worker/tests/...
    tests/known-failures/scripts.txt   scripts/tests/...
    tests/known-failures/web.txt       apps/web/tests/...

    Format: <test id or test file> | <ticket> | <owner> | <since YYYY-MM-DD> | <why it fails today>

A run (scripts/test-tier.sh, scripts/web-unit-ci.py, CI) FAILS when:
  * a test fails that no baseline line names (a NEW failure);
  * a baseline line's test ran to the end and did not fail (STALE: delete the line, the list
    only shrinks);
  * a SECURITY GUARD fails (tests/tiers.yaml `security:`) — always, listed or not. A security
    guard can never sit in a baseline: loading a baseline that names one is an error.

    python3 scripts/known_failures.py check                  every baseline is well formed
    python3 scripts/known_failures.py files --suites a,b     the test files the baselines name
    python3 scripts/known_failures.py judge --ran F --failed F [--passed F] [--unfinished F]
                                            [--suites a,b]
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
BASELINES = ROOT / "tests" / "known-failures"

#: suite -> the directory its test files live in (repository-relative, with a trailing slash).
SUITES: dict[str, str] = {
    "engine": "engine/tests/",
    "api": "apps/api/tests/",
    "worker": "apps/worker/tests/",
    "scripts": "scripts/tests/",
    "web": "apps/web/tests/",
}

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TICKET = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


@dataclass(frozen=True)
class Entry:
    id: str          # a test id (file::test) or a whole test file
    ticket: str
    owner: str
    since: str
    reason: str
    line: int

    @property
    def file(self) -> str:
        return self.id.split("::", 1)[0]

    def covers(self, failed_id: str) -> bool:
        """A whole-file entry covers every test of that file; a test id covers itself and its
        parametrised cases (`test_x` covers `test_x[a]`)."""
        if "::" not in self.id:
            return failed_id == self.id or failed_id.startswith(self.id + "::")
        return failed_id == self.id or failed_id.startswith(self.id + "[")


def suite_of(path: str) -> str | None:
    for suite, prefix in SUITES.items():
        if path.startswith(prefix):
            return suite
    return None


def security_files(root: Path = ROOT) -> set[str]:
    """The security guards (tests/tiers.yaml `security:`): a red one fails every gate."""
    import yaml

    data = yaml.safe_load((root / "tests" / "tiers.yaml").read_text(encoding="utf-8")) or {}
    files = data.get("security") or []
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        raise ValueError("tests/tiers.yaml: `security:` must be a list of test files")
    return set(files)


def load(suite: str, root: Path = ROOT, *, security: set[str] | None = None) -> dict[str, Entry]:
    """{id: Entry} for one suite. Raises ValueError naming the line for anything malformed:
    a wrong field count, a test outside the suite, a missing file, a duplicate, an empty field,
    a bad ticket or date, or a security guard (which may never be baselined)."""
    path = root / "tests" / "known-failures" / f"{suite}.txt"
    if not path.exists():
        return {}
    security = security_files(root) if security is None else security
    out: dict[str, Entry] = {}
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        where = f"tests/known-failures/{suite}.txt:{n}"
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 5 or not all(parts):
            raise ValueError(f"{where}: expected '<test> | <ticket> | <owner> | <since> | <why>'")
        test, ticket, owner, since, reason = parts
        if suite_of(test) != suite:
            raise ValueError(f"{where}: {test} is not under {SUITES[suite]}")
        if not (root / test.split("::", 1)[0]).exists():
            raise ValueError(f"{where}: {test.split('::', 1)[0]} does not exist: delete the line")
        if not _TICKET.match(ticket):
            raise ValueError(f"{where}: ticket {ticket!r} is not like BOSSAPP-123")
        if not _DATE.match(since):
            raise ValueError(f"{where}: since {since!r} is not YYYY-MM-DD")
        if len(reason.split()) < 3:
            raise ValueError(f"{where}: say why it fails in words")
        if test.split("::", 1)[0] in security:
            raise ValueError(f"{where}: {test} is a security guard; a red security guard is "
                             "fixed, never baselined")
        if test in out:
            raise ValueError(f"{where}: {test} is listed twice")
        out[test] = Entry(test, ticket, owner, since, reason, n)
    return out


def load_all(root: Path = ROOT, suites: Iterable[str] = SUITES) -> dict[str, dict[str, Entry]]:
    security = security_files(root)
    return {s: load(s, root, security=security) for s in suites}


def judge(*, ran: Iterable[str], failed: Iterable[str], unfinished: Iterable[str] = (),
          passed: Iterable[str] | None = None,
          baselines: Mapping[str, Mapping[str, Entry]], security: set[str]
          ) -> tuple[list[str], list[str]]:
    """(problems, known): every reason the run fails, as sentences, and the known failures seen.

    `ran` is the test FILES that ran; `failed` the failed test ids (or a file, or a sentence for
    a run that crashed); `unfinished` the files whose run never finished (their entries cannot
    be judged stale); `passed` the test ids (or, for the web suite, files) that PASSED. With
    `passed`, a line is stale only when a test it covers passed: a test SKIPPED here (no Go
    toolchain, no Apache AGE) is not evidence that it is fixed."""
    ran, unfinished = set(ran), set(unfinished)
    passed_ids = None if passed is None else {p for p in passed if p}
    failed = sorted({f for f in failed if f})
    entries = [e for suite in baselines.values() for e in suite.values()]
    problems: list[str] = []
    known: list[str] = []
    for f in failed:
        file = f.split("::", 1)[0]
        hit = next((e for e in entries if e.covers(f)), None)
        if file in security:
            problems.append(f"SECURITY GUARD RED: {f} (a security guard is fixed, never "
                            "baselined)")
        elif hit is None:
            problems.append(f"NEW FAILURE: {f} (not in tests/known-failures/)")
        else:
            known.append(f"{f} ({hit.ticket}, {hit.owner})")
    for e in entries:
        if e.file not in ran or e.file in unfinished or any(e.covers(f) for f in failed):
            continue
        suite = suite_of(e.id)
        if passed_ids is None or any(e.covers(p) for p in passed_ids):
            problems.append(f"STALE: {e.id} passes now ({e.ticket}): delete line {e.line} of "
                            f"tests/known-failures/{suite}.txt (the list only shrinks)")
        else:
            # Neither passed nor failed: skipped here, or renamed away. A line nobody can judge
            # would sit forever, so it is refused too: fix the test or delete the line.
            problems.append(f"NOT JUDGED: {e.id} neither failed nor passed here (skipped, or no "
                            f"such test) ({e.ticket}): fix it or delete line {e.line} of "
                            f"tests/known-failures/{suite}.txt")
    return problems, known


def _lines(path: str | None) -> list[str]:
    if not path or not Path(path).exists():
        return []
    return [l.strip() for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    fl = sub.add_parser("files")
    fl.add_argument("--suites", default=",".join(SUITES))
    jd = sub.add_parser("judge")
    jd.add_argument("--ran", required=True)
    jd.add_argument("--failed", required=True)
    jd.add_argument("--unfinished")
    jd.add_argument("--passed", help="the passed test ids, one per line (pytest -rA)")
    jd.add_argument("--suites", default=",".join(SUITES))
    args = ap.parse_args(argv)
    try:
        if args.cmd == "check":
            sizes = {s: len(b) for s, b in load_all().items()}
            print("known failures per suite: " + ", ".join(f"{s} {n}" for s, n in sizes.items()))
            return 0
        suites = [s.strip() for s in args.suites.split(",") if s.strip()]
        unknown = [s for s in suites if s not in SUITES]
        if unknown:
            raise ValueError(f"unknown suite(s): {', '.join(unknown)}")
        baselines = load_all(suites=suites)
    except ValueError as error:
        print(f"known-failures: {error}", file=sys.stderr)
        return 2
    if args.cmd == "files":
        files = sorted({e.file for b in baselines.values() for e in b.values()})
        print("\n".join(files))
        return 0
    problems, known = judge(ran=_lines(args.ran), failed=_lines(args.failed),
                            unfinished=_lines(args.unfinished),
                            passed=_lines(args.passed) if args.passed else None,
                            baselines=baselines,
                            security=security_files())
    print(f"known failures (listed, not counted): {len(known)}")
    for k in known:
        print("  " + k)
    print(f"problems: {len(problems)}")
    for p in problems:
        print("  " + p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
