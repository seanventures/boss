#!/usr/bin/env python3
"""BOSS-97 dry-run load harness (boss_engine.services.go_scale.dry_run_report), for operators.

It runs the in-process Python stub soak and prints its report as JSON. It spends nothing and
needs no Firecracker. A live run (`--live`, real model calls) or a Firecracker run
(`--firecracker`) is refused: no Go runtime is shipped (go_scale.GO_SCALE_TRACK), so this
harness has nothing honest to drive there.

    python scripts/go_scale_dry_run.py --n 20 --concurrency 4 --duration-ms 0
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n", type=int, default=20, help="tasks to run")
    parser.add_argument("--concurrency", type=int, default=4, help="most tasks in flight at once")
    parser.add_argument("--duration-ms", type=float, default=0.0, help="how long each stub task takes")
    parser.add_argument("--live", action="store_true", help="refused: this harness does not spend")
    parser.add_argument("--firecracker", action="store_true",
                        help="refused: no Firecracker runner is shipped")
    args = parser.parse_args(argv)

    if args.live:
        print("refused: the dry run does not spend; there is no live mode (no model calls).",
              file=sys.stderr)
        return 2
    if args.firecracker:
        print("refused: no Go runtime or Firecracker runner is shipped; the dry run is the "
              "Python stub only.", file=sys.stderr)
        return 2

    from boss_engine.services.go_scale import dry_run_report

    report = dry_run_report(n=args.n, concurrency=args.concurrency,
                            duration_s=max(0.0, args.duration_ms) / 1000.0)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
