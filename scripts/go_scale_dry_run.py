#!/usr/bin/env python3
"""BOSS-97 dry-run load harness.

Measures the Python scheduling/concurrency stub. Does not spend, does not
require Firecracker, and refuses any live/Go soak flag.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
ENGINE = ROOT / "engine"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

FORBIDDEN = ("--live", "--firecracker", "--spend", "--soak")


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    hit = [flag for flag in FORBIDDEN if flag in raw]
    if hit:
        print(
            "go_scale_dry_run: refuse "
            + ", ".join(hit)
            + " — this harness does not spend and does not start Firecracker.",
            file=sys.stderr,
        )
        return 2

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--duration-ms", type=int, default=0)
    args = parser.parse_args(raw)

    from boss_engine.services.go_scale import dry_run_report

    report = dry_run_report(
        n=max(0, args.n),
        concurrency=max(1, args.concurrency),
        duration_s=max(0.0, args.duration_ms) / 1000.0,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
