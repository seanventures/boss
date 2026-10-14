"""Print where a build's time and tokens went.

    python -m boss_engine.tools.build_profile <run_id> [--json] [--database-url URL]
    python -m boss_engine.tools.build_profile <run_id> --rows rows.json

Reads the database named by --database-url or DATABASE_URL, inside a READ ONLY transaction.
`--rows` reads an export instead: {"turns": [...], "llm_calls": [...], "run": {...}}, each row
shaped like its table's columns (services/build_profile.load_rows).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from ..services.build_profile import format_profile, load_rows, profile_from_rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m boss_engine.tools.build_profile",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("run_id")
    parser.add_argument("--json", action="store_true", help="print the profile as JSON")
    parser.add_argument("--rows", help="compute from an exported rows JSON file, not the database")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    args = parser.parse_args(argv)

    if args.rows:
        with open(args.rows, encoding="utf-8") as fh:
            rows = json.load(fh)
    else:
        if not args.database_url:
            parser.error("set DATABASE_URL or pass --database-url (or use --rows)")
        from sqlalchemy import create_engine, text

        engine = create_engine(args.database_url, future=True)
        with engine.connect() as c:
            c.execute(text("SET TRANSACTION READ ONLY"))
            rows = load_rows(c, args.run_id)
            c.rollback()
    if not rows.get("run") and not rows.get("turns"):
        print(f"no run or turns found for {args.run_id}", file=sys.stderr)
        return 1
    profile = profile_from_rows(rows.get("turns") or [], rows.get("llm_calls") or [],
                                rows.get("run"), run_id=args.run_id,
                                swallowed=rows.get("swallowed") or [])
    print(json.dumps(profile, indent=2, default=str) if args.json else format_profile(profile))
    return 0


if __name__ == "__main__":
    sys.exit(main())
