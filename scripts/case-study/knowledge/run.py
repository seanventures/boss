#!/usr/bin/env python3
"""NorthPeak knowledge-graph case study harness.

This process does not call a model, clone Saleor, or reserve budget.
`--approve-spend` records that an operator asked, and still exits before
any smoke stage. Primary criteria are unmet until a later run writes
artifacts and a report from real builds.
"""
from __future__ import annotations

import argparse
import sys

ALLOWED_ARMS = {"A", "B"}
ALLOWED_ENGINES = {"E1", "E2"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Knowledge graph case study")
    parser.add_argument("--arm", required=True, choices=sorted(ALLOWED_ARMS))
    parser.add_argument("--engine", required=True, choices=sorted(ALLOWED_ENGINES))
    parser.add_argument("--tasks", required=True)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--approve-spend", action="store_true")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        print("repeat must be >= 1", file=sys.stderr)
        return 2
    if not args.approve_spend:
        print(
            "refusing: the case study spends model credit. "
            "Pass --approve-spend after operator approval. No model call was made."
        )
        return 2
    print(
        "refusing: --approve-spend was set but this build does not execute the smoke stage. "
        "No model call was made."
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
