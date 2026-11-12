"""Build procedural action priors (BOSSAPP-1031) from a database's agent_turns and print JSON.

Offline and replay-first: point DATABASE_URL (or --database-url) at any database that holds
transcripts — a bench database, a replica — and the fold upserts kg_action_priors for the org.
Idempotent: a high-water mark per org means a re-run never double-counts; --since overrides it
for a deliberate re-fold.

    DATABASE_URL=postgresql+psycopg://... python3 scripts/build_action_priors.py --org org_boss
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))

from sqlalchemy import create_engine  # noqa: E402

from boss_engine.db import ensure_schema  # noqa: E402
from boss_engine.services.knowledge.procedural_priors import build_priors  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--org", default=os.environ.get("BOSS_DEFAULT_ORG_ID") or "org_boss",
                        help="org the priors are built for (default: the install's org)")
    parser.add_argument("--since", default=None,
                        help="ISO time to fold from, overriding the high-water mark")
    parser.add_argument("--database-url", default=None,
                        help="overrides DATABASE_URL for the replay database")
    args = parser.parse_args()
    url = args.database_url or os.environ.get("DATABASE_URL") or ""
    if not url:
        raise SystemExit("DATABASE_URL (or --database-url) must name the replay database")
    since = None
    if args.since:
        since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
    engine = create_engine(url, future=True)
    ensure_schema(engine)  # idempotent: the deploy's own migration path
    print(json.dumps(build_priors(engine, org_id=args.org, since=since), indent=2))


if __name__ == "__main__":
    main()
