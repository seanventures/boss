#!/usr/bin/env python3
"""The web unit suite as CI runs it: every file, and a quarantine that can only shrink.

    python3 scripts/web-unit-ci.py              run the whole suite (apps/web, vitest) and judge it
    python3 scripts/web-unit-ci.py --only-quarantined
                                                run just the quarantined files: each must fail

The quarantine (tests/web-quarantine.txt) lists the files that fail on main, each with its ticket
and the reason. The run FAILS when:
  * a file fails that is not in the quarantine (a new failure), or
  * a quarantined file passes (it was fixed: delete its line, so the list only shrinks), or
  * a quarantined file no longer exists.
Nothing is hidden: every quarantined failure is printed with its ticket on every run.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps" / "web"
QUARANTINE = ROOT / "tests" / "web-quarantine.txt"


def read_quarantine(path: Path = QUARANTINE) -> dict[str, dict[str, str]]:
    """{file under apps/web: {ticket, reason}}. Raises ValueError on a malformed line."""
    out: dict[str, dict[str, str]] = {}
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 3 or not all(parts):
            raise ValueError(f"{path.name}:{n}: expected '<file> | <ticket> | <reason>'")
        file, ticket, reason = parts
        if file in out:
            raise ValueError(f"{path.name}:{n}: {file} is listed twice")
        out[file] = {"ticket": ticket, "reason": reason}
    return out


def run_vitest(files: list[str]) -> dict[str, str]:
    """{file under apps/web: 'passed' | 'failed'} from vitest's JSON report."""
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "vitest.json"
        cmd = ["npx", "vitest", "run", "--reporter=dot", "--reporter=json",
               f"--outputFile.json={report}", *files]
        subprocess.run(cmd, cwd=WEB, check=False)
        if not report.exists():
            raise SystemExit("web-unit-ci: vitest wrote no JSON report (did it start at all?)")
        data = json.loads(report.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for suite in data.get("testResults") or []:
        name = Path(suite["name"]).resolve()
        try:
            rel = name.relative_to(WEB.resolve()).as_posix()
        except ValueError:
            rel = str(name)
        failed = suite.get("status") == "failed" or any(
            a.get("status") == "failed" for a in suite.get("assertionResults") or [])
        out[rel] = "failed" if failed else "passed"
    return out


def judge(results: dict[str, str], quarantine: dict[str, dict[str, str]]) -> list[str]:
    """Every reason the run fails, as sentences."""
    problems = []
    failing = {f for f, s in results.items() if s == "failed"}
    for f in sorted(failing - set(quarantine)):
        problems.append(f"NEW FAILURE: {f} (not in tests/web-quarantine.txt)")
    for f, q in sorted(quarantine.items()):
        if not (WEB / f).exists():
            problems.append(f"QUARANTINED FILE IS GONE: {f} ({q['ticket']}): delete its line")
        elif results.get(f) == "passed":
            problems.append(f"QUARANTINED FILE NOW PASSES: {f} ({q['ticket']}): delete its line")
        elif f not in results:
            problems.append(f"QUARANTINED FILE DID NOT RUN: {f} ({q['ticket']})")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only-quarantined", action="store_true")
    args = ap.parse_args(argv)
    quarantine = read_quarantine()
    files = sorted(quarantine) if args.only_quarantined else []
    results = run_vitest(files)
    if not results:
        print("web-unit-ci: no test file ran", file=sys.stderr)
        return 1
    failing = sorted(f for f, s in results.items() if s == "failed")
    # ONE visible retry for an UNEXPECTED failure. The whole suite is 9,000 tests on a loaded
    # runner, and a 5 s per-test timeout trips there on files that pass alone in seconds
    # (tests/control-plane-visible.test.ts, 2026-11-10). A new failure that also fails alone is
    # real and still fails the job; a file that passes its retry is reported as FLAKY, never
    # hidden. Quarantined files get no retry: they are expected to fail.
    unexpected = [f for f in failing if f not in quarantine and (WEB / f).exists()]
    if unexpected and not args.only_quarantined:
        print(f"\nweb-unit-ci: retrying {len(unexpected)} unexpected failure(s) alone: "
              + ", ".join(unexpected))
        retry = run_vitest(unexpected)
        for f, status in retry.items():
            if f in unexpected and status == "passed":
                print(f"  FLAKY: {f} failed in the full run and passed alone")
                results[f] = "passed"
        failing = sorted(f for f, s in results.items() if s == "failed")
    print(f"\nweb-unit-ci: {len(results)} files ran, {len(failing)} failed, "
          f"{len(quarantine)} quarantined")
    for f in failing:
        q = quarantine.get(f)
        if q:
            print(f"  quarantined  {f}  [{q['ticket']}] {q['reason']}")
    problems = judge(results, quarantine)
    for p in problems:
        print(f"  {p}")
    print("web-unit-ci: " + ("FAIL" if problems else "PASS (only quarantined files failed)"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
