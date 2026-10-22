#!/usr/bin/env python3
"""Write the case-study report from artifacts.

With no artifacts, the report says the primary criteria were not run.
It does not invent solve counts or costs.
"""
from __future__ import annotations

import argparse
from pathlib import Path


def render(artifact_dir: Path) -> str:
    results = list(artifact_dir.glob("**/results.json")) if artifact_dir.is_dir() else []
    if not results:
        return (
            "# Knowledge graph case study\n\n"
            "Primary criteria: not run.\n\n"
            "No result files were present. Solve counts and costs are not estimated.\n"
        )
    return (
        "# Knowledge graph case study\n\n"
        f"Result files: {len(results)}\n\n"
        "This renderer does not score the primary criteria. A scored report is a later step.\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    text = render(args.artifacts)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
